import bottle
import datetime
import hmac
import json
import logging
import os
import threading
import time

import cloze_config
import job_store

# Max in-memory request body. Bodies above this are rejected by bottle before
# add_doc() sees them, so it is the real upload ceiling. Comfortably covers a
# few hundred standard pages of UTF-8; note that a body near this size can take
# a long time to arrive, which is what AVER_GUNICORN_TIMEOUT has to absorb.
bottle.BaseRequest.MEMFILE_MAX = 10 * 1024 * 1024  # 10 MB

logging.basicConfig(level=os.getenv("AVER_LOG_LEVEL", "INFO"))
logger = logging.getLogger("aver.api")

# Configuration
DB_PATH = os.getenv("AVER_DB_PATH", "./jobs.db")
JOB_TTL_SECONDS = int(os.getenv("AVER_JOB_TTL_SECONDS", str(7 * 24 * 3600)))
MAX_QUEUE = int(os.getenv("AVER_MAX_QUEUE", "0"))  # 0 = unbounded
API_KEY = os.getenv("AVER_API_KEY")  # unset => auth disabled

# Target candidate count under the current CLASSES_NUM. Read once at import
# so a mid-flight env change does not retroactively change the target for
# already-enqueued jobs; each job carries its own copy in the DB row. The
# backend and the worker on this deployment share WebAPI/.env, so they see
# the same value; if that ever changes, the worker's actual quota is the
# one that runs -- this value is only advertisement.
_ACCEPTED_TARGET = cloze_config.get_accepted_target()

# Backend process start time. Reported by /health so operators can spot a
# recently-restarted web tier.
_BACKEND_STARTED_AT = time.time()

# /health thresholds. Kept as env vars so operators can tune per host without
# a code change.
HEALTH_HEARTBEAT_STALE_SECONDS = float(os.getenv(
    "AVER_HEALTH_HEARTBEAT_STALE_SECONDS", "60"))
HEALTH_FAILURES_1H_ALERT = int(os.getenv(
    "AVER_HEALTH_FAILURES_1H_ALERT", "10"))
HEALTH_STUCK_PROCESSING_SECONDS = float(os.getenv(
    "AVER_HEALTH_STUCK_PROCESSING_SECONDS", "18000"))    # 5 h; a normal job
# on the CPU deployment takes 2-3 h, so 5 h is "definitely stuck".
HEALTH_QUEUE_BACKLOG_ALERT_SECONDS = float(os.getenv(
    "AVER_HEALTH_QUEUE_BACKLOG_ALERT_SECONDS", "172800"))  # 48 h. A single
# worker processing 2-3 h per job can legitimately keep a job queued behind
# several others for many hours; only flag as degraded once a queued job has
# been waiting more than two full days, which is deep in "someone must look
# at this" territory.

# Per-process SQLite connection, now shared by that process's threads: the web
# tier runs the gthread worker class (see start_api.sh), so several requests can
# be in flight in one process. job_store.connect opens it with
# check_same_thread=False and serializes writes behind its own lock; the lock
# here only stops two threads from racing on the lazy first open.
_conn = None
_conn_lock = threading.Lock()


def get_conn():
    global _conn
    if _conn is None:
        with _conn_lock:
            if _conn is None:
                conn = job_store.connect(DB_PATH)
                job_store.init_db(conn)
                _conn = conn
    return _conn


# Routes reachable without an API key. /health is public on purpose so
# uptime monitors (UptimeRobot etc.) can poll it without a shared secret.
_PUBLIC_ROUTES = frozenset({"/health"})


@bottle.hook("before_request")
def _require_api_key():
    """Static API-key check on every endpoint. Disabled when AVER_API_KEY unset."""
    if bottle.request.path in _PUBLIC_ROUTES:
        return
    if not API_KEY:
        return
    provided = bottle.request.get_header("X-API-Key")
    if not provided or not hmac.compare_digest(provided, API_KEY):
        raise bottle.HTTPError(401, "Invalid or missing API key.")


'''
POST /document  -- submit a document for analysis (asynchronous).

Request body (unchanged from the old synchronous API):
{
  "language": "en",
  "document_id": "223e4567-e89b-12d3-a456-426614174001",
  "paragraphs": [
    {"id": "123e...", "content": "This is an example paragraph."},
    {"id": "423e...", "content": "Another paragraph."}
  ]
}

Returns 202 Accepted with a job id. Poll GET /jobs/<id> for status and fetch the
result from GET /jobs/<id>/result once the state is "finished".
'''


@bottle.post('/document')
def add_doc():
    next_document = bottle.request.json
    logger.info("POST /document content-length=%s", bottle.request.content_length)
    if next_document is None:
        logger.warning("Request JSON is missing or invalid.")
        return bottle.HTTPError(400, "Invalid or missing JSON body.")
    for key in ("language", "document_id", "paragraphs"):
        if key not in next_document:
            return bottle.HTTPError(400, f"Missing required key: {key}")

    conn = get_conn()
    if MAX_QUEUE and job_store.count_active(conn) >= MAX_QUEUE:
        bottle.response.status = 429
        bottle.response.set_header("Retry-After", "60")
        return {"error": "Queue is full, please retry later."}

    job_id = job_store.enqueue(conn, next_document, JOB_TTL_SECONDS,
                               accepted_target=_ACCEPTED_TARGET)
    job = job_store.get_job(conn, job_id)
    bottle.response.status = 202
    bottle.response.set_header("Location", f"/jobs/{job_id}")
    return {
        "job_id": job_id,
        "state": job_store.QUEUED,
        "status_url": f"/jobs/{job_id}",
        "expires_at": job["expires_at"],
    }


@bottle.get('/jobs/<job_id>')
def job_status(job_id):
    job = job_store.get_job(get_conn(), job_id)
    if job is None:
        return bottle.HTTPError(404, "Job not found.")
    return {
        "job_id": job_id,
        "state": job["state"],
        # ``done``/``total`` count LM inference batches (unit is "batch of
        # BATCH_SIZE candidates"), so ``done/total`` is a good ETA proxy but
        # is not directly comparable to the number of returned words.
        # ``accepted`` is the count of candidates the language model has
        # accepted so far -- monotonic during a run, and directly comparable
        # to the target quota (default 15, or AVER_CLOZE_TOTAL). Random-fill
        # candidates from the last-resort branch are *not* counted here.
        "progress": {
            "done": job["progress_done"],
            "total": job["progress_total"],
            "accepted": job["accepted"],
            # Target this job is aiming for (usually 15, or whatever
            # AVER_CLOZE_TOTAL / AVER_CLASSES_NUM was set to at enqueue
            # time). NULL only on rows migrated in before this column
            # existed.
            "total_accepted": job["accepted_target"],
        },
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "expires_at": job["expires_at"],
        "error": job["error"],
    }


@bottle.post('/jobs/<job_id>/cancel')
def cancel_job(job_id):
    """Cancel a job. Queued jobs are cancelled immediately; a processing job is
    flagged and the worker stops it at its next batch."""
    conn = get_conn()
    if job_store.get_job(conn, job_id) is None:
        return bottle.HTTPError(404, "Job not found.")
    result = job_store.request_cancel(conn, job_id)
    if result is None:
        return bottle.HTTPError(404, "Job not found.")
    if result == job_store.CANCELLED:
        msg = "Job cancelled."
    elif result == "cancelling":
        msg = "Cancellation requested; the worker will stop it shortly."
    else:
        msg = f"Job already {result}; nothing to cancel."
    return {"job_id": job_id, "cancel": result, "message": msg}


@bottle.get('/jobs/<job_id>/result')
def job_result(job_id):
    job = job_store.get_job(get_conn(), job_id)
    if job is None:
        return bottle.HTTPError(404, "Job not found.")
    state = job["state"]
    if state in (job_store.QUEUED, job_store.PROCESSING):
        bottle.response.status = 409
        bottle.response.set_header("Retry-After", "30")
        return {"job_id": job_id, "state": state, "message": "Result not ready yet."}
    if state == job_store.FAILED:
        return {"job_id": job_id, "state": job_store.FAILED, "error": job["error"]}
    if state == job_store.CANCELLED:
        return {"job_id": job_id, "state": job_store.CANCELLED, "message": "Job was cancelled."}
    # finished
    return json.loads(job["result_json"])


'''
POST /similarity  -- evaluate whether two texts match (synchronous, fast).

A pair is judged by cosine similarity against the configured threshold and the
verdict is returned in the grading schema: result "yes" (match) or "no" (no
match). The endpoint always responds with HTTP 200; a malformed request yields
result "fail".

Single pair:
    {"text1": "a sentence", "text2": "another sentence"}
    -> {"result": "yes", "text": "..."}

Batch (a JSON array, one result object per pair, in order):
    {"pairs": [{"text1": "...", "text2": "..."}, ...]}
    -> [{"result": "yes", "text": "..."}, {"result": "no", "text": "..."}, ...]

A pair missing string text1/text2 yields {"result": "fail", ...} for that entry.
'''


def _verdict(score):
    """Map a cosine score to a grading-schema result object."""
    import similarity
    if similarity.is_match(score):
        return {"result": "yes", "text": "Answer matches."}
    return {"result": "no", "text": "Answer does not match."}


def _valid_pair(p):
    return isinstance(p, dict) and isinstance(p.get("text1"), str) and isinstance(p.get("text2"), str)


@bottle.post('/similarity')
def similarity_endpoint():
    body = bottle.request.json
    if body is None:
        return {"result": "fail", "text": "Invalid or missing JSON body."}
    try:
        import similarity
        if "pairs" in body:
            pairs = body["pairs"]
            if not isinstance(pairs, list):
                return {"result": "fail", "text": "'pairs' must be a list."}
            # Evaluate the valid pairs in one batch; flag invalid ones in place.
            results = [None] * len(pairs)
            valid_idx, valid_pairs = [], []
            for i, p in enumerate(pairs):
                if _valid_pair(p):
                    valid_idx.append(i)
                    valid_pairs.append((p["text1"], p["text2"]))
                else:
                    results[i] = {"result": "fail", "text": "Each pair needs string 'text1' and 'text2'."}
            for i, score in zip(valid_idx, similarity.similarity_batch(valid_pairs)):
                results[i] = _verdict(score)
            bottle.response.content_type = "application/json"
            return json.dumps(results)  # top-level JSON array
        if _valid_pair(body):
            return _verdict(similarity.similarity(body["text1"], body["text2"]))
        return {"result": "fail", "text": "Provide string 'text1' and 'text2', or 'pairs'."}
    except ImportError:
        logger.exception("sentence-transformers is not installed.")
        return {"result": "fail", "text": "Similarity model unavailable (sentence-transformers not installed)."}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Similarity computation failed.")
        return {"result": "fail", "text": f"Similarity computation failed: {exc}"}


def _build_health():
    """Assemble the /health payload. Split out so tests can call it directly."""
    now = time.time()
    problems = []

    # ---- DB probe: separate from any long-lived connection so a corrupted
    # SQLite file also flags degraded. Keep the query trivial.
    db_info = {"reachable": False, "path": DB_PATH}
    try:
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        db_info["reachable"] = True
    except Exception as exc:  # noqa: BLE001
        db_info["error"] = f"{type(exc).__name__}: {exc}"
        problems.append("database unreachable")

    worker_info = {"alive": False, "state": "unknown", "current_job_id": None,
                   "seconds_since_heartbeat": None, "process_uptime_seconds": None}
    queue_info = {"queued": 0, "processing": 0,
                  "oldest_queued_seconds": None, "oldest_processing_seconds": None}
    failures_info = {"last_1h": 0, "last_24h": 0}

    if db_info["reachable"]:
        try:
            ws = job_store.worker_state_read(conn)
        except Exception as exc:  # noqa: BLE001
            ws = None
            problems.append(f"worker_state read failed: {type(exc).__name__}")
        if ws is None:
            problems.append("worker has never started")
        else:
            age = max(0.0, now - ws["updated_at"])
            alive = age <= HEALTH_HEARTBEAT_STALE_SECONDS
            worker_info.update({
                "alive": alive,
                "state": ws["state"],
                "current_job_id": ws["current_job_id"],
                "seconds_since_heartbeat": round(age, 3),
                "process_uptime_seconds": round(now - ws["process_started_at"], 1),
                "version": ws.get("version"),
            })
            if not alive:
                problems.append(
                    f"worker heartbeat is {age:.0f}s old "
                    f"(threshold {HEALTH_HEARTBEAT_STALE_SECONDS:.0f}s)")
            if ws["state"] == "starting" and age > HEALTH_HEARTBEAT_STALE_SECONDS:
                # Distinct case: heartbeat is stale AND it never got to idle.
                problems.append("worker stuck in 'starting'")

        try:
            counts = job_store.count_by_state(conn)
        except Exception as exc:  # noqa: BLE001
            counts = {}
            problems.append(f"queue count failed: {type(exc).__name__}")
        queue_info["queued"] = counts.get(job_store.QUEUED, 0)
        queue_info["processing"] = counts.get(job_store.PROCESSING, 0)

        try:
            oq = job_store.oldest_in_state_seconds(conn, job_store.QUEUED)
            op = job_store.oldest_in_state_seconds(conn, job_store.PROCESSING)
            queue_info["oldest_queued_seconds"] = None if oq is None else round(oq, 1)
            queue_info["oldest_processing_seconds"] = None if op is None else round(op, 1)
            if op is not None and op > HEALTH_STUCK_PROCESSING_SECONDS:
                problems.append(
                    f"job stuck in processing for {op:.0f}s "
                    f"(threshold {HEALTH_STUCK_PROCESSING_SECONDS:.0f}s)")
            if oq is not None and oq > HEALTH_QUEUE_BACKLOG_ALERT_SECONDS:
                problems.append(
                    f"queue backlog: oldest queued job is {oq:.0f}s old "
                    f"(threshold {HEALTH_QUEUE_BACKLOG_ALERT_SECONDS:.0f}s)")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"queue age query failed: {type(exc).__name__}")

        try:
            failures_info["last_1h"] = job_store.count_failed_since(conn, 3600)
            failures_info["last_24h"] = job_store.count_failed_since(conn, 86400)
            if failures_info["last_1h"] >= HEALTH_FAILURES_1H_ALERT:
                problems.append(
                    f"{failures_info['last_1h']} failed job(s) in the last hour "
                    f"(threshold {HEALTH_FAILURES_1H_ALERT})")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"failure-count query failed: {type(exc).__name__}")

    status_ok = not problems
    payload = {
        "status": "ok" if status_ok else "degraded",
        "checked_at": datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "uptime": {"backend_seconds": round(now - _BACKEND_STARTED_AT, 1)},
        "db": db_info,
        "worker": worker_info,
        "queue": queue_info,
        "recent_failures": failures_info,
        "problems": problems,
    }
    return status_ok, payload


@bottle.get('/health')
def health():
    """Public health check. GET-only; no API key required.

    Returns HTTP 200 + status:"ok" when everything looks fine, HTTP 503 +
    status:"degraded" when at least one probe raised a concern. The full
    payload always ships so operators can see *why* the endpoint reports
    degraded. Design for uptime monitors:

    * Uptime monitor: watch for HTTP 200. Any non-2xx response is a fault.
    * Content match: search response body for the literal string
      ``"status":"ok"`` for belt-and-suspenders coverage against partial
      infrastructure failures that still return 200 (unlikely but cheap).

    Thresholds are env-tunable (see AVER_HEALTH_* in .env.example). The
    endpoint deliberately does not reveal per-request contents.
    """
    ok, payload = _build_health()
    bottle.response.status = 200 if ok else 503
    bottle.response.content_type = "application/json"
    return json.dumps(payload)


@bottle.get('/quit/<pid>')
def quit(pid):
    if pid != str(os.getpid()):
        return '<html><body>permission denied</body></html>'
    os._exit(2)


app = bottle.default_app()


def parseargs():
    import argparse
    parser = argparse.ArgumentParser(description="AVER web API")
    parser.add_argument('--port', type=int, default=int(os.getenv("AVER_PORT", "11122")))
    return parser.parse_args()


if __name__ == '__main__':
    args = parseargs()
    get_conn()
    bottle.run(debug=True, reloader=True, host='0.0.0.0', port=args.port)
