#!/usr/bin/env python
"""
AVer inference worker.

A single long-running process that owns the MT5 model and processes jobs from
the SQLite queue strictly one at a time, using all available CPU. The web tier
(bottleAPI) only enqueues jobs and reads results; all heavy inference happens
here. See ../README.md for the operator-facing overview.

Run via start_worker.sh (sets thread env), or directly:

    AVER_DB_PATH=./jobs.db ./venv/bin/python worker.py
"""

import datetime
import inspect
import json
import logging
import os
import signal
import subprocess
import time

import cloze_config
import gpu
import job_store
from cloze_config import PICK_CLASSES, load_classes_num, scale_classes_num  # noqa: F401 (re-exported for tests)

logging.basicConfig(level=os.getenv("AVER_LOG_LEVEL", "INFO"))
logger = logging.getLogger("aver.worker")

DB_PATH = os.getenv("AVER_DB_PATH", "./jobs.db")
POLL_SECONDS = float(os.getenv("AVER_WORKER_POLL_SECONDS", "2"))
# Run a cleanup sweep roughly this often while idle (seconds).
CLEANUP_INTERVAL = float(os.getenv("AVER_WORKER_CLEANUP_INTERVAL", "3600"))
# Eval workers stop after their current inference batch. Production keeps its
# prior behavior and finishes the active document before shutdown.
STOP_AFTER_BATCH = os.getenv(
    "AVER_STOP_AFTER_BATCH", "0").lower() in ("1", "true", "yes", "on")

# LM decision log: one JSON line per LM decision (accept / spare / reject /
# reject_dup) with the full top-K predictions. Fuel for a future pre-filter
# / re-ranker classifier and for auditing recurrent rejects. Set the env var
# to an empty string to disable. See docs/agents/domain.md for the schema.
LM_DECISION_LOG_PATH = os.getenv("AVER_LM_DECISION_LOG", "./lm_decisions.jsonl")

# When enabled, the worker checks the working tree's git HEAD on every idle
# poll. If the SHA has moved since the worker booted, it exits gracefully
# between jobs and lets systemd start a fresh process on the new code -- so
# an operator's whole update workflow becomes just `git pull` (no restart
# needed, and any in-flight job finishes untouched first). Off by default.
AUTO_RESTART_ON_NEW_COMMIT = os.getenv(
    "AVER_AUTO_RESTART_ON_NEW_COMMIT", "0").lower() in ("1", "true", "yes", "on")


def _current_commit():
    """Return the short git SHA of the working tree, or None if unavailable.

    Fails silently (no git, not a git checkout, timeout, ...) so the feature
    is safe to enable on hosts that don't ship the code from git.
    """
    workdir = os.path.dirname(os.path.abspath(__file__)) or "."
    try:
        out = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--short", "HEAD"],
            capture_output=True, check=True, timeout=5, text=True,
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None

# Cloze-class configuration lives in ``cloze_config`` so the web tier can
# read it too without dragging in the ML deps. See that module for
# PICK_CLASSES / _DEFAULT_CLASSES_NUM / load_classes_num.
# _DEFAULT_CLASSES_NUM is re-exported here for tests that still import it.
_DEFAULT_CLASSES_NUM = cloze_config._DEFAULT_CLASSES_NUM


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


CLASSES_NUM = load_classes_num()
MIN_RANGE = _env_int("AVER_MIN_RANGE", 1)
# Drives beam search width, number of returned sequences, and the acceptance
# window in _get_ml_pos: a candidate is accepted only if the LM ranks it within
# positions [MIN_RANGE, MAX_RANGE). 41 => "within the first 40".
MAX_RANGE = _env_int("AVER_MAX_RANGE", 41)
BATCH_SIZE = _env_int("AVER_BATCH_SIZE", 5)

_running = True


class JobCancelled(Exception):
    """Raised cooperatively when a job's cancel flag is set mid-processing."""


def _handle_stop(signum, frame):
    global _running
    logger.info("Received signal %s, finishing current job then exiting.", signum)
    _running = False


class _DecisionLog:
    """Append-only JSONL sink for LM decisions.

    Opened once per worker process, one line per candidate the LM was actually
    invoked on. Line-buffered so a hard kill loses at most the last line.
    Failures never propagate to the job path -- if writing breaks (disk full,
    permission), we log once and swallow further errors.
    """

    def __init__(self, path):
        self.path = path
        self._fh = None
        self._broken = False

    def _open(self):
        if self._fh is None and not self._broken:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".",
                            exist_ok=True)
                # buffering=1 = line-buffered when opened in text mode.
                self._fh = open(self.path, "a", buffering=1, encoding="utf8")
                logger.info("LM decision log: %s", self.path)
            except OSError as exc:
                logger.warning("Cannot open LM decision log %r (%s); "
                               "decision logging disabled.", self.path, exc)
                self._broken = True

    def build_callback(self, job_id, language, applied_language):
        """Return a decision_cb closure suitable for get_plausible_words."""
        self._open()
        if self._broken or self._fh is None:
            return None

        def cb(word, decision, ml_pos, predictions):
            # Written outside the LM's hot path. Keep it small and total (no
            # exceptions to the caller).
            try:
                row = {
                    "ts": datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                    "job_id": job_id,
                    "language_declared": language,
                    "language_applied": applied_language,
                    "cloze_class": getattr(word, "cloze_class", None),
                    "num_of_words": len(getattr(word, "word", "").split()),
                    "word": word.word,
                    "word_class": getattr(word, "word_class", ""),
                    "paragraph_index": word.paragraph_index,
                    "sentence_index": word.sentence_index,
                    "token_index": word.token_index,
                    "sentence": word.sentence,
                    "blanked_sentence": word.blanked_sentence,
                    "decision": decision,
                    "ml_pos": ml_pos,
                    "predictions": predictions,
                }
                self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001 - never fail the job for logging
                if not self._broken:
                    logger.exception("LM decision log write failed; disabling.")
                    self._broken = True
                    try:
                        self._fh.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._fh = None

        return cb

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
        self._fh = None


def _load_analyzer():
    from MLmethod.MT5_model_method import DocumentAnalyzer
    logger.info("Loading analyzer...")
    analyzer = DocumentAnalyzer()
    supports_progress = "progress_cb" in inspect.signature(analyzer.get_plausible_words).parameters
    logger.info("Analyzer loaded (progress reporting: %s)", supports_progress)
    return analyzer, supports_progress


def process_job(conn, analyzer, supports_progress, job, decision_log=None):
    """Run the full analysis for one job and return the response payload."""
    job_id = job["id"]
    document = json.loads(job["request_json"])
    language = document["language"]
    document_id = document["document_id"]
    paragraphs = document["paragraphs"]

    paragraph_id_array = [p["id"] for p in paragraphs]
    content = [p["content"] for p in paragraphs]

    logger.info("Job %s: scanning document (%d paragraphs, lang=%s)",
                job_id, len(paragraphs), language)
    analyzer.scan_document(content, language, PICK_CLASSES)
    # The analyzer may have auto-corrected the language via the detector in
    # MLmethod/language_detection.py (env AVER_LANGUAGE_AUTO_CORRECT, off by
    # default; enable in WebAPI/.env or a systemd drop-in). Pick up its
    # verdict so we can put it in the response payload and log it once per
    # job. The `isinstance` guard keeps unit tests that pass a MagicMock
    # analyzer from hitting this path with a Mock attribute.
    lc_attr = getattr(analyzer, "language_correction", None)
    language_correction = lc_attr if isinstance(lc_attr, dict) else None
    if language_correction and language_correction.get("applied") != language:
        logger.warning("Job %s: language auto-corrected declared=%s -> applied=%s "
                       "(detected=%s, margin=%s)",
                       job_id, language_correction["declared"],
                       language_correction["applied"],
                       language_correction.get("detected"),
                       language_correction.get("margin"))
    if job_store.is_cancel_requested(conn, job_id):     # cancelled during scan
        raise JobCancelled()

    kwargs = {"batch_size": BATCH_SIZE}

    # Running LM-accepted count. Mutable box so the closures below share it.
    # A list-of-one is the standard cheap trick before Python 3-style
    # `nonlocal`; also plays well with the wrapper decision_cb.
    accepted_count = [0]

    if supports_progress:
        def _progress(done, total):
            job_store.update_progress(conn, job_id, done, total,
                                      accepted=accepted_count[0])
            if STOP_AFTER_BATCH and not _running:
                raise JobCancelled()
            if job_store.is_cancel_requested(conn, job_id):
                raise JobCancelled()                     # stop at this batch
        kwargs["progress_cb"] = _progress

    # get_plausible_words accepts decision_cb; probe by signature so tests
    # (and any future analyzer variant) that lack the parameter still work.
    params = inspect.signature(analyzer.get_plausible_words).parameters
    if "decision_cb" in params:
        applied_lang = (
            language if not (language_correction and language_correction.get("applied"))
            else language_correction["applied"])
        log_cb = (decision_log.build_callback(job_id, language, applied_lang)
                  if decision_log is not None else None)

        def _decision_cb(word, decision, ml_pos, predictions):
            # Count LM-accepted candidates. Step C random-fill in the
            # analyzer does not fire decision_cb, which is what we want:
            # "accepted" here means "the LM said yes", not "the API will
            # return this word".
            if decision == "accept":
                accepted_count[0] += 1
            if log_cb is not None:
                log_cb(word, decision, ml_pos, predictions)

        kwargs["decision_cb"] = _decision_cb

    plausible_words = analyzer.get_plausible_words(MIN_RANGE, MAX_RANGE, CLASSES_NUM, **kwargs)
    # Final push: Step B accepts can fire after the last progress_cb tick,
    # so the DB row would otherwise miss them until the next state change.
    try:
        job_store.update_progress(conn, job_id,
                                  # Keep the last-known progress numbers; only
                                  # accepted may have moved.
                                  done=job_store.get_job(conn, job_id)["progress_done"],
                                  total=job_store.get_job(conn, job_id)["progress_total"],
                                  accepted=accepted_count[0])
    except Exception:
        logger.exception("Final accepted-count update failed; continuing.")
    logger.info("Job %s: %d plausible words found (LM accepted=%d)",
                job_id, len(plausible_words), accepted_count[0])

    words = []
    for word in plausible_words:
        # A candidate is "random" iff it came from the last-resort random-fill
        # branch in get_plausible_words. Those words were not verified by the
        # language model, so the frontend applies different thresholds to them.
        # The synthetic "random" category is only present in the response when
        # at least one word triggered the fallback; the original cloze class
        # is preserved in "origin_category" for callers that still need it.
        is_random = getattr(word, "random_fallback", False)
        payload = {
            "paragraph_id": paragraph_id_array[word.paragraph_index],
            "index": word.token_index,
            "content": word.word,
            "sentence_blanked": word.blanked_sentence,
            "sentence_index": word.sentence_index,
            "predictions": word.predictions,
            # Space-separated Universal POS tags for each token of the
            # n-gram, in surface order. Stored on AnalyzedWord as
            # `word_class` (the second positional arg of the ctor); empty
            # string on old records where sentence_extract_ngrams did not
            # populate the tag list correctly.
            "pos_tags": getattr(word, "word_class", "") or "",
            # Cloze class shown to the frontend. Real cloze classes come from
            # PICK_CLASSES (e.g. "unigrams_ADJ"). The special value "random"
            # marks last-resort random-fill candidates.
            "category": "random" if is_random else getattr(word, "cloze_class", None),
            "additional_info": "",
        }
        if is_random:
            # PICK_CLASSES key the candidate would have belonged to if the LM
            # had accepted it. Lets the frontend fall back to per-class
            # rendering while still knowing the candidate is random.
            payload["origin_category"] = getattr(word, "cloze_class", None)
        words.append(payload)
    response = {"document_id": document_id, "words": words}
    # Include the detector verdict so callers can see what happened, and so
    # aver_db.py can surface it out of the stored result_json. Absent only
    # when the analyzer did not run (e.g. legacy code path).
    if language_correction is not None:
        response["language_correction"] = language_correction
    return response


def main():
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    conn = job_store.connect(DB_PATH)
    job_store.init_db(conn)

    # Crash recovery: anything still 'processing' is orphaned from a previous run.
    job_store.reset_orphans(conn)
    job_store.cleanup_expired(conn)

    # Publish "starting" as early as possible so /health can distinguish
    # "worker is warming up" (loading MT5 can take minutes) from "worker is
    # dead". Do this before the analyzer load rather than after.
    process_started_at = time.time()
    # Version reported on /health and used by the auto-restart-on-new-commit
    # feature. Priority: explicit AVER_VERSION env override -> git SHA of the
    # working tree -> None. Frozen for the process's lifetime; the idle-tick
    # comparison re-queries git each time.
    boot_commit = os.getenv("AVER_VERSION") or _current_commit()
    if AUTO_RESTART_ON_NEW_COMMIT:
        logger.info("Auto-restart on new commit: enabled (boot SHA=%s).",
                    boot_commit or "unknown")
    job_store.worker_state_upsert(
        conn, "starting", process_started_at=process_started_at,
        version=boot_commit)

    analyzer, supports_progress = _load_analyzer()
    use_gpu = gpu.gpu_enabled()
    logger.info("GPU acquisition per job: %s", "enabled" if use_gpu else "disabled (CPU only)")
    logger.info("CLASSES_NUM=%s (sum=%d) MIN_RANGE=%s MAX_RANGE=%s BATCH_SIZE=%s",
                CLASSES_NUM, sum(CLASSES_NUM.values()), MIN_RANGE, MAX_RANGE, BATCH_SIZE)
    decision_log = _DecisionLog(LM_DECISION_LOG_PATH) if LM_DECISION_LOG_PATH else None
    if decision_log is None:
        logger.info("LM decision log: disabled (AVER_LM_DECISION_LOG is empty).")
    job_store.worker_state_upsert(conn, "idle")
    logger.info("Worker ready, polling for jobs (db=%s).", DB_PATH)

    last_cleanup = time.time()
    while _running:
        try:
            job = job_store.claim_next(conn)
        except Exception:
            logger.exception("Failed to claim a job; retrying after poll interval.")
            time.sleep(POLL_SECONDS)
            continue

        if job is None:
            # Idle tick: refresh the heartbeat so /health can see we are alive
            # even between jobs. Cheap (one UPDATE).
            try:
                job_store.worker_state_upsert(conn, "idle")
            except Exception:
                logger.exception("Idle heartbeat update failed; continuing.")
            # Between-jobs update check: if the working tree has moved since we
            # booted, exit gracefully and let systemd start us on the new code.
            # We check only when idle so an in-flight job is never interrupted.
            if AUTO_RESTART_ON_NEW_COMMIT and boot_commit:
                latest = _current_commit()
                if latest and latest != boot_commit:
                    logger.info(
                        "New commit detected (boot=%s, disk=%s); exiting so "
                        "systemd restarts the worker on the new code.",
                        boot_commit, latest)
                    _running = False
                    continue
            if time.time() - last_cleanup > CLEANUP_INTERVAL:
                job_store.cleanup_expired(conn)
                last_cleanup = time.time()
            time.sleep(POLL_SECONDS)
            continue

        job_id = job["id"]
        logger.info("Job %s: started.", job_id)
        start = time.time()
        try:
            job_store.worker_state_upsert(conn, "processing", current_job_id=job_id)
        except Exception:
            logger.exception("Processing heartbeat update failed; continuing.")

        # Acquire a GPU for the duration of this job (if enabled). If all GPUs
        # are busy, gpu.acquire blocks (retrying every 10 min) until one frees
        # up; _running lets a shutdown signal interrupt that wait.
        if use_gpu:
            device = gpu.acquire(should_continue=lambda: _running)
            if device is not None:
                analyzer.move_to_device(device)
            elif not _running:
                # Shutdown signalled while waiting for a free GPU. Surface the
                # claimed job as failed (consistent with crash recovery — there
                # is no auto-retry; the user resubmits) and stop.
                job_store.mark_failed(conn, job_id, "Worker shut down while waiting for a free GPU.")
                break

        try:
            result = process_job(conn, analyzer, supports_progress, job,
                                 decision_log=decision_log)
            job_store.mark_finished(conn, job_id, result)
            logger.info("Job %s: done in %.1fs.", job_id, time.time() - start)
        except JobCancelled:
            logger.info("Job %s: cancelled after %.1fs.", job_id, time.time() - start)
            job_store.mark_cancelled(conn, job_id)
        except Exception as exc:  # noqa: BLE001 - any failure must surface as 'failed'
            logger.exception("Job %s: failed.", job_id)
            job_store.mark_failed(conn, job_id, f"{type(exc).__name__}: {exc}")
        finally:
            # Release the GPU so it's free while we're idle between jobs.
            if use_gpu:
                analyzer.release_device()
                gpu.release()
            try:
                job_store.worker_state_upsert(conn, "idle")
            except Exception:
                logger.exception("Post-job heartbeat update failed; continuing.")

    try:
        job_store.worker_state_upsert(conn, "shutting_down")
    except Exception:
        logger.exception("Shutdown heartbeat update failed; continuing.")
    if decision_log is not None:
        decision_log.close()
    logger.info("Worker stopped.")


if __name__ == "__main__":
    main()
