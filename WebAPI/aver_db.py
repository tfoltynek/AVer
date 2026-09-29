#!/usr/bin/env python3
"""aver_db.py — inspect the AVer job queue and history.

The queue and every finished job live in an SQLite database written by
bottleAPI.py and worker.py (schema in job_store.py). This utility reads
that database directly. It has no dependency on Bottle, gunicorn, or
the AVer venv — plain system python3 is enough.

Subcommands:
    ls          list recent jobs (state / age / size / duration)
    queue       show only queued and processing jobs
    stats       throughput + latency percentiles (p0..p99) + queue wait
    failed      jobs that failed or produced an error
    show        full details of one job (times, paragraphs, category tallies)
    paragraphs  list the paragraphs in a job's request
    candidates  print every candidate the worker returned for a job
    leaks       scan a job's result for candidates that contain a stopword
    retag       recompute POS tags for a job's candidates using nltk
    pool        replay extraction with LM_verify off, dump n-gram pool
    request     dump raw request JSON
    result      dump raw result JSON
    search      find jobs whose request contains a substring

Examples:
    python3 aver_db.py ls --since 24
    python3 aver_db.py stats --since 168
    python3 aver_db.py queue
    python3 aver_db.py failed --since 168
    python3 aver_db.py show 432f4d48
    python3 aver_db.py candidates 432f4d48
    python3 aver_db.py leaks 432f4d48
    python3 aver_db.py search "Sugar2sugar"

Any job id may be given as a unique prefix (default: first 8 hex chars).
Point --db somewhere else if jobs.db is not at the default location.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import datetime

DEFAULT_DB = os.getenv("AVER_DB_PATH", "./jobs.db")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def open_db(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit(f"database not found: {path} (set AVER_DB_PATH or pass --db)")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _short(job_id: str, n: int = 10) -> str:
    return job_id[:n]


def _ts(v):
    if v is None:
        return "-"
    return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")


def _dur(started, finished):
    if started is None:
        return None
    end = finished if finished is not None else time.time()
    return end - started


def _fmt_dur(s):
    if s is None:
        return "-"
    if s < 60:
        return f"{s:5.1f}s"
    if s < 3600:
        return f"{s / 60:5.1f}m"
    return f"{s / 3600:5.1f}h"


def _fmt_bytes(n):
    if n is None:
        return "-"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / 1024 / 1024:.1f}M"


def _match_prefix(conn: sqlite3.Connection, prefix: str) -> str:
    """Resolve a short id prefix to a full job id. Fail if ambiguous."""
    rows = list(conn.execute(
        "SELECT id FROM jobs WHERE id LIKE ? || '%' LIMIT 2", (prefix,)))
    if not rows:
        sys.exit(f"no job matches id prefix {prefix!r}")
    if len(rows) > 1:
        sys.exit(f"prefix {prefix!r} is ambiguous")
    return rows[0]["id"]


def _walk_candidates(x, out):
    """Recursively collect every candidate dict (has 'content' + 'category')."""
    if isinstance(x, dict):
        if isinstance(x.get("content"), str) and "category" in x:
            out.append(x)
        for v in x.values():
            _walk_candidates(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk_candidates(v, out)


def _load_stopwords(path: str | None) -> set[str]:
    if path is None:
        for candidate in (
            "./MLmethod/stopwords_tagger/english_stopwords.txt",
            "../MLmethod/stopwords_tagger/english_stopwords.txt",
            "/app/AVer/MLmethod/stopwords_tagger/english_stopwords.txt",
        ):
            if os.path.exists(candidate):
                path = candidate
                break
    if path is None or not os.path.exists(path):
        # Minimal fallback: the closed-class words most likely to leak.
        return {
            "the", "an", "a", "of", "at", "by", "to", "for", "from",
            "in", "on", "with", "is", "are", "was", "were", "be",
            "been", "am", "these", "this", "those", "that", "and",
            "or", "but", "not", "if", "as",
        }
    with open(path, encoding="utf8") as f:
        return {w.strip().lower() for w in f if w.strip()}


def _pct(sorted_values, p: float):
    if not sorted_values:
        return None
    k = int(round((len(sorted_values) - 1) * p / 100))
    return sorted_values[k]


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_ls(args, conn):
    where = ["1=1"]
    params: list = []
    if args.state:
        where.append("state = ?")
        params.append(args.state)
    if args.since is not None:
        where.append("created_at >= ?")
        params.append(time.time() - args.since * 3600)
    if args.until is not None:
        where.append("created_at <= ?")
        params.append(time.time() - args.until * 3600)
    params.append(args.limit)
    rows = list(conn.execute(
        f"""SELECT id, state, created_at, started_at, finished_at,
                   length(request_json) req, length(result_json) res, error
            FROM jobs WHERE {' AND '.join(where)}
            ORDER BY created_at DESC LIMIT ?""",
        params))
    if not rows:
        print("(no jobs match)")
        return
    hdr = (f"{'id':10s}  {'state':10s}  {'created':19s}  "
           f"{'duration':>8s}  {'req':>6s}  {'res':>6s}  err")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        dur = _dur(r["started_at"], r["finished_at"])
        err = (r["error"] or "").split("\n", 1)[0][:40]
        print(f"{_short(r['id']):10s}  {r['state']:10s}  {_ts(r['created_at']):19s}  "
              f"{_fmt_dur(dur):>8s}  {_fmt_bytes(r['req']):>6s}  "
              f"{_fmt_bytes(r['res']):>6s}  {err}")


def cmd_queue(args, conn):
    rows = list(conn.execute(
        """SELECT id, state, created_at, started_at, worker_heartbeat,
                  progress_done, progress_total,
                  length(request_json) req
           FROM jobs WHERE state IN ('queued','processing')
           ORDER BY created_at ASC"""))
    if not rows:
        print("(queue empty)")
        return
    now = time.time()
    hdr = (f"{'id':10s}  {'state':10s}  {'created':19s}  {'age':>7s}  "
           f"{'req':>6s}  {'progress':>10s}  {'heartbeat':>12s}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        age = now - r["created_at"]
        prog = f"{r['progress_done']}/{r['progress_total']}"
        if r["worker_heartbeat"] is None:
            hb = "-"
        else:
            hb = f"{now - r['worker_heartbeat']:.0f}s ago"
        print(f"{_short(r['id']):10s}  {r['state']:10s}  {_ts(r['created_at']):19s}  "
              f"{_fmt_dur(age):>7s}  {_fmt_bytes(r['req']):>6s}  "
              f"{prog:>10s}  {hb:>12s}")


def cmd_stats(args, conn):
    where = "1=1"
    params: list = []
    if args.since is not None:
        where = "created_at >= ?"
        params.append(time.time() - args.since * 3600)

    # State histogram
    print("== job counts by state ==")
    total = 0
    for r in conn.execute(
        f"SELECT state, COUNT(*) c FROM jobs WHERE {where} GROUP BY state "
        f"ORDER BY c DESC", params):
        print(f"  {r['state']:12s} {r['c']:6d}")
        total += r["c"]
    print(f"  {'TOTAL':12s} {total:6d}")

    # Processing latency (started -> finished)
    rows = list(conn.execute(
        f"""SELECT started_at, finished_at FROM jobs
            WHERE {where} AND state='finished'
              AND started_at IS NOT NULL AND finished_at IS NOT NULL""",
        params))
    durs = sorted(r["finished_at"] - r["started_at"] for r in rows)
    if durs:
        print()
        print(f"== processing latency (n={len(durs)}) ==")
        for p in (0, 50, 90, 95, 99, 100):
            label = "max" if p == 100 else f"p{p}"
            print(f"  {label:4s} {_fmt_dur(_pct(durs, p))}")
        print(f"  mean {_fmt_dur(statistics.mean(durs))}")
    else:
        print("\n(no finished jobs in window)")

    # Queue wait (created -> started)
    rows = list(conn.execute(
        f"""SELECT created_at, started_at FROM jobs
            WHERE {where} AND started_at IS NOT NULL""",
        params))
    waits = sorted(r["started_at"] - r["created_at"] for r in rows)
    if waits:
        print()
        print(f"== queue wait time (n={len(waits)}) ==")
        for p in (0, 50, 90, 95, 100):
            label = "max" if p == 100 else f"p{p}"
            print(f"  {label:4s} {_fmt_dur(_pct(waits, p))}")

    # Request size
    rows = list(conn.execute(
        f"SELECT length(request_json) req FROM jobs WHERE {where}", params))
    sizes = sorted(r["req"] for r in rows)
    if sizes:
        print()
        print("== request size ==")
        for p in (0, 50, 90, 95, 100):
            label = "max" if p == 100 else f"p{p}"
            print(f"  {label:4s} {_fmt_bytes(_pct(sizes, p))}")

    # Throughput
    rows = list(conn.execute(
        f"""SELECT MIN(created_at) t0, MAX(finished_at) t1, COUNT(*) n
            FROM jobs WHERE {where} AND state='finished'""",
        params))
    if rows and rows[0]["n"] and rows[0]["t0"] and rows[0]["t1"]:
        span = max(1.0, rows[0]["t1"] - rows[0]["t0"])
        rate = rows[0]["n"] / span * 3600
        print()
        print(f"== throughput ==")
        print(f"  span      {_fmt_dur(span)}  ({rows[0]['n']} finished jobs)")
        print(f"  rate      {rate:.2f} jobs/hour")


def cmd_failed(args, conn):
    where = ["(state='failed' OR error IS NOT NULL)"]
    params: list = []
    if args.since is not None:
        where.append("created_at >= ?")
        params.append(time.time() - args.since * 3600)
    params.append(args.limit)
    rows = list(conn.execute(
        f"""SELECT id, state, created_at, finished_at, error
            FROM jobs WHERE {' AND '.join(where)}
            ORDER BY created_at DESC LIMIT ?""",
        params))
    if not rows:
        print("(no failed jobs in window)")
        return
    for r in rows:
        print(f"{_short(r['id'])}  state={r['state']:10s}  "
              f"created={_ts(r['created_at'])}  finished={_ts(r['finished_at'])}")
        if r["error"]:
            for line in r["error"].splitlines():
                print(f"    {line}")
        print()


def cmd_show(args, conn):
    jid = _match_prefix(conn, args.id)
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if r is None:
        sys.exit("no such job")

    req = json.loads(r["request_json"])
    res = json.loads(r["result_json"]) if r["result_json"] else None
    print(f"id         : {r['id']}")
    print(f"state      : {r['state']}")
    print(f"created    : {_ts(r['created_at'])}")
    print(f"started    : {_ts(r['started_at'])}")
    print(f"finished   : {_ts(r['finished_at'])}")
    print(f"expires    : {_ts(r['expires_at'])}")
    print(f"duration   : {_fmt_dur(_dur(r['started_at'], r['finished_at']))}")
    print(f"progress   : {r['progress_done']}/{r['progress_total']}")
    print(f"cancelled  : {'yes' if r['cancel_requested'] else 'no'}")
    if r["error"]:
        print("ERROR:")
        for line in r["error"].splitlines():
            print(f"    {line}")
    print()
    print(f"request    : {_fmt_bytes(len(r['request_json']))}")
    print(f"  language     : {req.get('language')}")
    print(f"  document_id  : {req.get('document_id')}")
    paragraphs = req.get("paragraphs", [])
    print(f"  paragraphs   : {len(paragraphs)}")
    for i, p in enumerate(paragraphs):
        preview = " ".join(p.get("content", "").split())[:80]
        print(f"    [{i}] id={p.get('id','?')} ({len(p.get('content',''))}B) {preview!r}")
    print()
    if res is None:
        print("result     : (none)")
        return
    print(f"result     : {_fmt_bytes(len(r['result_json']))}")
    lc = res.get("language_correction") if isinstance(res, dict) else None
    if lc:
        applied = lc.get("applied")
        declared = lc.get("declared")
        detected = lc.get("detected")
        if not lc.get("auto_correct_enabled", True):
            note = "auto-correct DISABLED"
        elif detected is None:
            note = "no signal, kept declared"
        elif applied != declared:
            note = f"AUTO-CORRECTED to {applied}"
        else:
            note = "matches declared"
        print(f"  language   : declared={declared} detected={detected} "
              f"applied={applied}  ({note})")
        scores = lc.get("scores")
        if scores:
            score_str = ", ".join(f"{k}={v:+.4f}" for k, v in scores.items())
            print(f"    scores   : {score_str}  margin={lc.get('margin', 0):.4f}  "
                  f"n_tokens={lc.get('n_tokens', 0)} n_chars={lc.get('n_chars', 0)}")
    cands: list = []
    _walk_candidates(res, cands)
    cats = Counter(c.get("category") for c in cands)
    origins = Counter(c.get("origin_category") for c in cands
                      if c.get("origin_category"))
    print(f"  candidates   : {len(cands)}")
    for cat, n in cats.most_common():
        print(f"    {str(cat):24s} {n}")
    if origins:
        print("  random-fallback origins:")
        for cat, n in origins.most_common():
            print(f"    {str(cat):24s} {n}")


def cmd_paragraphs(args, conn):
    jid = _match_prefix(conn, args.id)
    r = conn.execute("SELECT request_json FROM jobs WHERE id=?", (jid,)).fetchone()
    if r is None:
        sys.exit("no such job")
    req = json.loads(r["request_json"])
    for i, p in enumerate(req.get("paragraphs", [])):
        content = p.get("content", "")
        preview = " ".join(content.split())[:160]
        print(f"[{i}] id={p.get('id','?')}  ({len(content)}B)")
        print(f"    {preview!r}")


def cmd_candidates(args, conn):
    jid = _match_prefix(conn, args.id)
    r = conn.execute("SELECT result_json FROM jobs WHERE id=?", (jid,)).fetchone()
    if r is None or r["result_json"] is None:
        sys.exit("job has no result")
    res = json.loads(r["result_json"])
    cands: list = []
    _walk_candidates(res, cands)
    for c in cands:
        cat = str(c.get("category"))
        if c.get("origin_category"):
            cat += f" (was {c['origin_category']})"
        tags = c.get("pos_tags") or ""
        tag_col = f"  [{tags}]" if tags else ""
        print(f"  [{cat:32s}] {c['content']!r}{tag_col}")
    print(f"\ntotal: {len(cands)}")
    if any(c.get("pos_tags") for c in cands):
        return
    print("\n(pos_tags absent from this record; run `retag <id>` to recompute them "
          "against the stored request using nltk)")


def cmd_leaks(args, conn):
    jid = _match_prefix(conn, args.id)
    r = conn.execute("SELECT result_json FROM jobs WHERE id=?", (jid,)).fetchone()
    if r is None or r["result_json"] is None:
        sys.exit("job has no result")
    stops = _load_stopwords(args.stopwords)
    res = json.loads(r["result_json"])
    cands: list = []
    _walk_candidates(res, cands)
    leaks = []
    for c in cands:
        toks = re.findall(r"[A-Za-z']+", c["content"].lower())
        hit = [t for t in toks if t in stops]
        if hit:
            leaks.append((hit, c))
    print(f"LEAKING candidates: {len(leaks)} / {len(cands)}\n")
    if not leaks:
        return
    for hit, c in leaks:
        print(f"  content       = {c['content']!r}")
        print(f"    category     = {c.get('category')}")
        if c.get("origin_category"):
            print(f"    origin       = {c['origin_category']}")
        print(f"    stopword hit = {hit}")
        if c.get("pos_tags"):
            print(f"    pos_tags     = {c['pos_tags']}")
        for k in ("sentence_blanked", "paragraph_id", "index"):
            if k in c:
                v = str(c[k])
                if len(v) > 200:
                    v = v[:200] + "..."
                print(f"    {k:16s}= {v}")
        print()
    if not any(c.get("pos_tags") for _, c in leaks):
        print("(pos_tags absent from this record; run `retag <id>` to compute them "
              "with the same tagger the worker used)")


def cmd_request(args, conn):
    jid = _match_prefix(conn, args.id)
    r = conn.execute("SELECT request_json FROM jobs WHERE id=?", (jid,)).fetchone()
    if r is None:
        sys.exit("no such job")
    print(r["request_json"])


def cmd_result(args, conn):
    jid = _match_prefix(conn, args.id)
    r = conn.execute("SELECT result_json FROM jobs WHERE id=?", (jid,)).fetchone()
    if r is None or r["result_json"] is None:
        sys.exit("job has no result")
    print(r["result_json"])


def cmd_pool(args, conn):
    """Replay a job's extraction locally and print every n-gram candidate.

    Skips LM verification and the whole worker pipeline. Uses the same
    tokenizer, POS tagger, stopword list, text-quality filters, and
    PICK_CLASSES as the running worker (imported from worker.py), so the
    output is exactly the pool that would have been handed to the LM.
    Useful for auditing what the analyzer thinks the bigram / trigram
    candidates are in a given document without waiting for MT5.

    Needs nltk (via the AVer venv). For Czech / Slovak requests it also
    needs the morphodita taggers on disk (loaded lazily).
    """
    try:
        import nltk  # noqa: F401
    except ImportError:
        sys.exit("nltk is required for `pool`. Run via the AVer venv:\n"
                 "    ./venv/bin/python aver_db.py pool " + args.id)

    # Import lazily so `python3 aver_db.py -h` still works without corpy.
    # The worker module carries PICK_CLASSES; the analyzer holds the logic.
    old_cwd = os.getcwd()
    webapi_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(webapi_dir)
    sys.path.insert(0, webapi_dir)
    try:
        from MLmethod.MT5_model_method import DocumentAnalyzer
        try:
            from worker import PICK_CLASSES  # canonical config
        except ImportError as e:
            sys.exit(f"cannot import PICK_CLASSES from worker.py: {e}")

        jid = _match_prefix(conn, args.id)
        row = conn.execute("SELECT request_json FROM jobs WHERE id=?",
                           (jid,)).fetchone()
        if row is None:
            sys.exit("no such job")
        req = json.loads(row["request_json"])
        lang = req.get("language", "en")
        paragraphs = [p.get("content", "") for p in req.get("paragraphs", [])]

        # Build an analyzer just enough to run scan_document. Skips __init__
        # (which would load MT5 + morphodita) and hand-loads only what the
        # extraction path needs.
        a = DocumentAnalyzer.__new__(DocumentAnalyzer)
        a.logger = logging.getLogger("aver_db.pool")
        a.logger.addHandler(logging.NullHandler())

        a.stopwords = {}
        for code, fname in (("en", "english_stopwords.txt"),
                            ("cs", "czech_stopwords.txt"),
                            ("sk", "slovak_stopwords.txt")):
            path = os.path.join("MLmethod", "stopwords_tagger", fname)
            if os.path.exists(path):
                with open(path, encoding="utf8") as f:
                    a.stopwords[code] = f.read().splitlines()
            else:
                a.stopwords[code] = []

        # Peek at what the detector would say so we can load the right
        # tagger *before* scan_document runs. The analyzer's scan_document
        # will re-run detection internally with the same inputs and reach
        # the same conclusion; we're not double-swapping.
        applied_lang = lang
        # Default off, matching the analyzer. Enable via WebAPI/.env or by
        # exporting AVER_LANGUAGE_AUTO_CORRECT=1 in the shell before running
        # ``pool`` locally.
        auto_correct = os.getenv(
            "AVER_LANGUAGE_AUTO_CORRECT", "0").strip().lower() in ("1", "true", "yes", "on")
        if auto_correct:
            from MLmethod.language_detection import detect_language_for_paragraphs
            det = detect_language_for_paragraphs(
                paragraphs, stopwords=a.stopwords, fallback=lang)
            if det.language and det.language != lang:
                print(f"[pool] language auto-correct: declared={lang} "
                      f"detected={det.language} margin={det.margin:.4f} "
                      f"scores={ {k: round(v,4) for k,v in det.scores.items()} }")
                applied_lang = det.language

        if applied_lang == "cs":
            import corpy.morphodita
            a.tagger_czech = corpy.morphodita.Tagger(
                "MLmethod/stopwords_tagger/czech-morfflex-pdt-161115-pos_only.tagger")
        elif applied_lang == "sk":
            import corpy.morphodita
            a.tagger_slovak = corpy.morphodita.Tagger(
                "MLmethod/stopwords_tagger/slovak-morfflex-pdt-170914-pos_only.tagger")

        # Run extraction. Fills a.picked_words[class] with AnalyzedWord
        # objects for every n-gram that passes _valid_word + POS + text
        # quality filters, in document (paragraph/sentence/token) order.
        # Frequency ranking normally happens later in get_plausible_words.
        a.scan_document(paragraphs, lang, PICK_CLASSES)
    finally:
        os.chdir(old_cwd)

    # ---- print, grouped by class, filtered by --class if requested ---------
    def norm_key(w):
        return (w.paragraph_index, w.sentence_index, w.token_index, w.word)

    total = 0
    for cls_name, cfg in PICK_CLASSES.items():
        if args.only and args.only != cls_name:
            continue
        items = a.picked_words.get(cls_name, [])
        if not items:
            print(f"== {cls_name}  (0 candidates)")
            print()
            continue
        # picked_words is already frequency-ordered; also list unique surfaces.
        surfaces = {}
        for w in items:
            surfaces.setdefault(w.word, []).append(w)
        header = f"== {cls_name}  ({len(items)} occurrences, " \
                 f"{len(surfaces)} unique surfaces)"
        print(header)
        for surface, occs in surfaces.items():
            tag_str = getattr(occs[0], "word_class", "")
            first = occs[0]
            location = f"p{first.paragraph_index}s{first.sentence_index}i{first.token_index}"
            extra = f"  (+{len(occs) - 1} more)" if len(occs) > 1 else ""
            print(f"  {surface!r:32s}  tags=[{tag_str}]  first={location}{extra}")
            if args.show_context:
                print(f"      sentence: {first.sentence!r}")
        total += len(items)
        print()
    print(f"total candidates across shown classes: {total}")


def cmd_retag(args, conn):
    """Re-tag a job's request with nltk and print POS tags for each candidate.

    Works retroactively on jobs that predate the pos_tags field on the
    response payload. Uses the same tagger the worker uses (nltk universal
    tagset), so on a broken host it exposes the tagger's actual output for
    the exact tokens that leaked. Requires nltk (available via the AVer venv).
    """
    try:
        import nltk  # noqa: F401
        from nltk.tokenize import word_tokenize
        from nltk import pos_tag
    except ImportError:
        sys.exit("nltk is required for `retag`. Run with the AVer venv, e.g.:\n"
                 "    ./venv/bin/python aver_db.py retag " + args.id)

    jid = _match_prefix(conn, args.id)
    row = conn.execute(
        "SELECT request_json, result_json FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    if row is None:
        sys.exit("no such job")
    if row["result_json"] is None:
        sys.exit("job has no result")

    req = json.loads(row["request_json"])
    res = json.loads(row["result_json"])
    lang = req.get("language", "en")
    if lang != "en":
        print(f"(warning: request language is {lang!r}; nltk pos_tag is EN-only. "
              f"Tags below may be nonsense for non-EN.)")

    paragraphs = {p["id"]: p["content"] for p in req.get("paragraphs", [])}
    cands: list = []
    _walk_candidates(res, cands)

    # Cache per-paragraph tagging to avoid re-running for every candidate.
    tag_cache: dict = {}

    def _tag_para(pid):
        if pid in tag_cache:
            return tag_cache[pid]
        text = paragraphs.get(pid, "")
        sents = nltk.sent_tokenize(text)
        tagged_sents = []
        for sent in sents:
            toks = word_tokenize(sent)
            tags = pos_tag(toks, tagset="universal")
            tagged_sents.append(tags)
        tag_cache[pid] = (sents, tagged_sents)
        return tag_cache[pid]

    stops = _load_stopwords(args.stopwords)

    def _stop_mask(tokens):
        return [t.lower() in stops for t in tokens]

    for c in cands:
        cat = str(c.get("category"))
        if c.get("origin_category"):
            cat += f" (was {c['origin_category']})"
        content = c["content"]
        pid = c.get("paragraph_id")
        tok_idx = c.get("index")
        sent_idx = c.get("sentence_index")

        header = f"[{cat}]  content={content!r}  paragraph={pid}  sent={sent_idx}  index={tok_idx}"
        print(header)

        if pid not in paragraphs or tok_idx is None:
            print("    (cannot locate: missing paragraph_id or index)\n")
            continue

        sents, tagged_sents = _tag_para(pid)

        # Number of surface tokens in the candidate. Use word_tokenize on the
        # candidate itself so we count the same way the analyzer did.
        n = len(word_tokenize(content))

        # Find a sentence + start index where the next n tokens match the
        # candidate's content. Prefer the sentence at sent_idx, then any.
        matched = None
        candidates_sent_order = [sent_idx] if isinstance(sent_idx, int) else []
        candidates_sent_order += [i for i in range(len(tagged_sents))
                                  if i not in candidates_sent_order]
        for si in candidates_sent_order:
            if si is None or si < 0 or si >= len(tagged_sents):
                continue
            tags = tagged_sents[si]
            for start in range(0, len(tags) - n + 1):
                window_words = [w for w, _ in tags[start:start + n]]
                if " ".join(window_words).lower() == " ".join(
                        word_tokenize(content)).lower():
                    matched = (si, start, tags[start:start + n])
                    break
            if matched:
                break

        if not matched:
            print(f"    (could not re-match {content!r} in tagged paragraph)\n")
            continue

        si, start, tagged_slice = matched
        words = [w for w, _ in tagged_slice]
        tags_ = [t for _, t in tagged_slice]
        stop_bits = _stop_mask(words)
        print(f"    sentence[{si}]: {sents[si]!r}")
        print(f"    tokens at [{start}:{start + n}]:")
        for w, t, s in zip(words, tags_, stop_bits):
            flag = " STOPWORD" if s else ""
            print(f"      {w!r:20s}  tag={t:<5s}{flag}")
        print()


def cmd_search(args, conn):
    rows = list(conn.execute(
        """SELECT id, state, created_at, length(request_json) req
           FROM jobs WHERE request_json LIKE ?
           ORDER BY created_at DESC LIMIT ?""",
        (f"%{args.term}%", args.limit)))
    if not rows:
        print("(no matches)")
        return
    for r in rows:
        print(f"  {_short(r['id']):10s}  {r['state']:10s}  "
              f"{_ts(r['created_at'])}  {_fmt_bytes(r['req']):>6s}")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="aver_db",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"path to jobs.db (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ls", help="list recent jobs")
    s.add_argument("--state", help="filter by state (queued|processing|finished|failed|expired)")
    s.add_argument("--since", type=float, help="only jobs created within N hours")
    s.add_argument("--until", type=float, help="only jobs created before N hours ago")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("queue", help="show queued and processing jobs")
    s.set_defaults(func=cmd_queue)

    s = sub.add_parser("stats", help="throughput and latency percentiles")
    s.add_argument("--since", type=float,
                   help="restrict window to last N hours (default: all-time)")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("failed", help="jobs that failed or set the error column")
    s.add_argument("--since", type=float)
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_failed)

    s = sub.add_parser("show", help="detailed view of one job")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("paragraphs", help="list paragraphs of a job's request")
    s.add_argument("id")
    s.set_defaults(func=cmd_paragraphs)

    s = sub.add_parser("candidates",
                       help="print every candidate the worker returned for a job")
    s.add_argument("id")
    s.set_defaults(func=cmd_candidates)

    s = sub.add_parser("leaks", help="scan a job's result for stopword-leaking candidates")
    s.add_argument("id")
    s.add_argument("--stopwords",
                   help="path to a stopword file (auto-detects English if omitted)")
    s.set_defaults(func=cmd_leaks)

    s = sub.add_parser("request", help="dump raw request JSON")
    s.add_argument("id")
    s.set_defaults(func=cmd_request)

    s = sub.add_parser("result", help="dump raw result JSON")
    s.add_argument("id")
    s.set_defaults(func=cmd_result)

    s = sub.add_parser("retag",
                       help="re-run the POS tagger on a job's stored request and "
                            "print tags for each candidate (needs nltk)")
    s.add_argument("id")
    s.add_argument("--stopwords",
                   help="path to a stopword file (auto-detects English if omitted)")
    s.set_defaults(func=cmd_retag)

    s = sub.add_parser("pool",
                       help="replay a job's extraction locally with LM_verify "
                            "disabled; prints every n-gram candidate the "
                            "analyzer would have handed to the LM (needs nltk)")
    s.add_argument("id")
    s.add_argument("--only",
                   help="show only this cloze class (e.g. trigrams_w_ADJ)")
    s.add_argument("--show-context", action="store_true",
                   help="also print the source sentence for each candidate")
    s.set_defaults(func=cmd_pool)

    s = sub.add_parser("search", help="find jobs whose request contains a substring")
    s.add_argument("term")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    args = p.parse_args()
    conn = open_db(args.db)
    args.func(args, conn)


if __name__ == "__main__":
    main()
