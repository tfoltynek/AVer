"""
SQLite-backed job store for the AVer async job queue.

A single SQLite file (WAL mode) holds all jobs. The web tier (bottleAPI) inserts
jobs and reads status/results; the single worker process (worker.py) claims and
processes them. See ../README.md for the operator-facing design overview.

Connections are per-process. Operations are short; a module-level lock guards
writes so the store is safe even when the web tier serves requests on threads.
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid

logger = logging.getLogger("aver.jobs")

# Job states
QUEUED = "queued"
PROCESSING = "processing"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"

DEFAULT_TTL_SECONDS = int(os.getenv("AVER_JOB_TTL_SECONDS", str(7 * 24 * 3600)))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    state            TEXT NOT NULL,
    progress_done    INTEGER NOT NULL DEFAULT 0,
    progress_total   INTEGER NOT NULL DEFAULT 0,
    -- Number of candidate words the language model has accepted so far. Set
    -- alongside progress_done on each batch tick, and once more right after
    -- the analyzer returns so Step B accepts that fire after the final batch
    -- land in the row too. Excludes Step C random-fill: that branch does not
    -- call decision_cb, which is the point of the counter -- it measures
    -- "real" (LM-verified) accepts only.
    accepted         INTEGER NOT NULL DEFAULT 0,
    -- Target accepted count captured at enqueue time from the current
    -- CLASSES_NUM (production default 15). Stable for the job's lifetime so
    -- callers can render "accepted/total_accepted" without knowing the
    -- current env. NULL only on rows migrated in from an older schema.
    accepted_target  INTEGER,
    request_json     TEXT NOT NULL,
    result_json      TEXT,
    error            TEXT,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL,
    expires_at       REAL NOT NULL,
    worker_heartbeat REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state   ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_expires ON jobs(expires_at);

-- Singleton row (id = 1) that the worker touches on every poll. Lets the
-- /health endpoint tell "worker up and idle" apart from "worker died N s ago".
CREATE TABLE IF NOT EXISTS worker_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    state               TEXT NOT NULL,          -- starting | idle | processing | shutting_down
    current_job_id      TEXT,
    updated_at          REAL NOT NULL,
    process_started_at  REAL NOT NULL,
    version             TEXT
);
"""

_write_lock = threading.Lock()


def connect(db_path):
    """Open a connection with WAL and sane pragmas. One per process."""
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(conn):
    with _write_lock:
        conn.executescript(_SCHEMA)
        # Migrate pre-existing DBs that predate later columns. Both these
        # ALTERs are guarded so a fresh DB (which already has the columns
        # from _SCHEMA) skips them cleanly.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        if "cancel_requested" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN cancel_requested "
                         "INTEGER NOT NULL DEFAULT 0")
        if "accepted" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN accepted "
                         "INTEGER NOT NULL DEFAULT 0")
        if "accepted_target" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN accepted_target INTEGER")
        conn.commit()


def enqueue(conn, request_obj, ttl_seconds=DEFAULT_TTL_SECONDS,
            accepted_target=None):
    """Insert a new queued job, return its id.

    ``accepted_target`` is the target candidate count under the *current*
    CLASSES_NUM (production 15). Stored so ``GET /jobs/<id>`` can show
    ``progress.total_accepted`` without the client hard-coding the default.
    NULL if the caller does not know it (e.g. tests, legacy code paths).
    """
    job_id = uuid.uuid4().hex
    now = time.time()
    with _write_lock:
        conn.execute(
            "INSERT INTO jobs (id, state, request_json, created_at, expires_at, "
            "accepted_target) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, QUEUED, json.dumps(request_obj), now, now + ttl_seconds,
             accepted_target),
        )
        conn.commit()
    logger.info("Enqueued job %s (expires in %ds, target=%s)",
                job_id, ttl_seconds, accepted_target)
    return job_id


def get_job(conn, job_id):
    """Return the job as a dict, or None if unknown or expired.

    Expired jobs are treated as absent (they return None) so callers get a 404
    even before the cleanup cron physically deletes them.
    """
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] is not None and time.time() >= row["expires_at"]:
        return None
    return dict(row)


def count_active(conn):
    """Number of queued or processing jobs (for backpressure)."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE state IN (?, ?)",
        (QUEUED, PROCESSING),
    ).fetchone()
    return row["c"]


def claim_next(conn):
    """Atomically claim the oldest queued job; mark it processing. Return it or None.

    Uses BEGIN IMMEDIATE so the select+update can't interleave. With a single
    worker this cannot race, but it stays correct regardless.
    """
    now = time.time()
    with _write_lock:
        try:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                "SELECT id FROM jobs WHERE state = ? ORDER BY created_at LIMIT 1",
                (QUEUED,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            job_id = row["id"]
            conn.execute(
                "UPDATE jobs SET state = ?, started_at = ?, worker_heartbeat = ? "
                "WHERE id = ?",
                (PROCESSING, now, now, job_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return get_job(conn, job_id)


def update_progress(conn, job_id, done, total, accepted=None):
    """Refresh the job's progress row.

    ``done``/``total`` are the LM-batch counters (batches processed / batches
    expected). ``accepted`` is the count of language-model-accepted candidates
    so far -- omit it to leave the previous value untouched, useful for
    callers that update progress and accepts at different cadences.
    """
    now = time.time()
    with _write_lock:
        if accepted is None:
            conn.execute(
                "UPDATE jobs SET progress_done = ?, progress_total = ?, "
                "worker_heartbeat = ? WHERE id = ?",
                (int(done), int(total), now, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET progress_done = ?, progress_total = ?, "
                "accepted = ?, worker_heartbeat = ? WHERE id = ?",
                (int(done), int(total), int(accepted), now, job_id),
            )
        conn.commit()


def mark_finished(conn, job_id, result_obj):
    now = time.time()
    with _write_lock:
        conn.execute(
            "UPDATE jobs SET state = ?, result_json = ?, finished_at = ? WHERE id = ?",
            (FINISHED, json.dumps(result_obj), now, job_id),
        )
        conn.commit()
    logger.info("Job %s finished", job_id)


def mark_failed(conn, job_id, error_message):
    now = time.time()
    with _write_lock:
        conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
            (FAILED, str(error_message), now, job_id),
        )
        conn.commit()
    logger.info("Job %s failed: %s", job_id, error_message)


def request_cancel(conn, job_id):
    """Request cancellation of a job. Returns a status string:
      - CANCELLED   : was queued, cancelled immediately (worker never runs it)
      - "cancelling": was processing, flagged; the worker stops at its next batch
      - finished/failed/cancelled : terminal already, nothing to do
      - None        : unknown job
    """
    now = time.time()
    with _write_lock:
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        state = row["state"]
        if state == QUEUED:
            conn.execute("UPDATE jobs SET state = ?, cancel_requested = 1, "
                         "finished_at = ? WHERE id = ?", (CANCELLED, now, job_id))
            conn.commit()
            logger.info("Job %s cancelled while queued", job_id)
            return CANCELLED
        if state == PROCESSING:
            conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
            conn.commit()
            logger.info("Job %s cancel requested while processing", job_id)
            return "cancelling"
        return state


def is_cancel_requested(conn, job_id):
    """Cheap read the worker calls between batches; sees the API's committed flag."""
    row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?",
                       (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def mark_cancelled(conn, job_id):
    now = time.time()
    with _write_lock:
        conn.execute("UPDATE jobs SET state = ?, finished_at = ? WHERE id = ?",
                     (CANCELLED, now, job_id))
        conn.commit()
    logger.info("Job %s cancelled", job_id)


def reset_orphans(conn):
    """Mark any 'processing' job as failed.

    Called on worker startup. Because there is exactly one worker, anything left
    in 'processing' is orphaned from a previous crashed/restarted run. We do not
    retry automatically (the user resubmits if they want to).
    """
    now = time.time()
    with _write_lock:
        cur = conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ? WHERE state = ?",
            (
                FAILED,
                "worker restarted during processing; please resubmit",
                now,
                PROCESSING,
            ),
        )
        conn.commit()
        n = cur.rowcount
    if n:
        logger.warning("Reset %d orphaned processing job(s) to failed", n)
    return n


def worker_state_upsert(conn, state, current_job_id=None,
                        process_started_at=None, version=None):
    """Refresh (or create) the singleton worker_state row.

    ``process_started_at`` and ``version`` are only written on the first call
    (row insertion). Subsequent calls preserve them. The bookkeeping fields
    (``state``, ``current_job_id``, ``updated_at``) are always overwritten.
    """
    now = time.time()
    with _write_lock:
        existing = conn.execute(
            "SELECT process_started_at, version FROM worker_state WHERE id = 1"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO worker_state "
                "(id, state, current_job_id, updated_at, process_started_at, version) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (state, current_job_id, now,
                 process_started_at if process_started_at is not None else now,
                 version),
            )
        else:
            conn.execute(
                "UPDATE worker_state SET state = ?, current_job_id = ?, "
                "updated_at = ? WHERE id = 1",
                (state, current_job_id, now),
            )
        conn.commit()


def worker_state_read(conn):
    """Return the worker_state row as a dict, or None if the worker never ran."""
    row = conn.execute(
        "SELECT state, current_job_id, updated_at, process_started_at, version "
        "FROM worker_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def count_by_state(conn):
    """Return a {state: count} mapping across all rows in ``jobs``.

    Used by the /health endpoint to expose queue depth without exposing any
    per-request contents.
    """
    rows = conn.execute("SELECT state, COUNT(*) AS c FROM jobs GROUP BY state").fetchall()
    return {r["state"]: r["c"] for r in rows}


def oldest_in_state_seconds(conn, state):
    """Age of the oldest row currently in the given state, or None if none.

    Age is measured against ``created_at`` for QUEUED and ``started_at`` for
    PROCESSING so the two numbers mean "waited this long" and "has been
    running this long" respectively.
    """
    col = "started_at" if state == PROCESSING else "created_at"
    row = conn.execute(
        f"SELECT MIN({col}) AS oldest FROM jobs WHERE state = ?", (state,)
    ).fetchone()
    if row is None or row["oldest"] is None:
        return None
    return max(0.0, time.time() - row["oldest"])


def count_failed_since(conn, seconds):
    """How many rows moved to state=failed in the last ``seconds`` seconds."""
    since = time.time() - seconds
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE state = ? AND finished_at >= ?",
        (FAILED, since),
    ).fetchone()
    return row["c"] if row else 0


def cleanup_expired(conn):
    """Delete jobs past their retention window. Return number deleted."""
    now = time.time()
    with _write_lock:
        cur = conn.execute("DELETE FROM jobs WHERE expires_at < ?", (now,))
        conn.commit()
        n = cur.rowcount
        if n:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    if n:
        logger.info("Cleanup purged %d expired job(s)", n)
    return n


if __name__ == "__main__":
    # CLI: `python job_store.py cleanup` — used by the cleanup cron.
    logging.basicConfig(level=os.getenv("AVER_LOG_LEVEL", "INFO"))
    db = os.getenv("AVER_DB_PATH", "./jobs.db")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cleanup"
    c = connect(db)
    init_db(c)
    if cmd == "cleanup":
        print("purged", cleanup_expired(c))
    elif cmd == "stats":
        rows = c.execute("SELECT state, COUNT(*) AS c FROM jobs GROUP BY state").fetchall()
        for r in rows:
            print(r["state"], r["c"])
    else:
        print("usage: python job_store.py [cleanup|stats]")
        sys.exit(2)
