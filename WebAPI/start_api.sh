#!/bin/sh

# Load local deployment config if present (see .env.example).
ENV_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$ENV_DIR/.env" ]; then
	set -a
	. "$ENV_DIR/.env"
	set +a
fi

export HF_HOME=./venv/huggingface

# The web tier is thin: it enqueues jobs and reads results from SQLite. The
# heavy MT5 inference runs in the separate worker (start_worker.sh). Keep the
# web threads small so they never steal CPU from the worker. The /similarity
# endpoint loads a small embedding model lazily in this process.
export OMP_NUM_THREADS=${AVER_WEB_THREADS:-1}

AVER_PORT=${AVER_PORT:-11122}
AVER_WEB_CONCURRENCY=${AVER_WEB_CONCURRENCY:-2}
# The timeout covers the WHOLE request, not just handler time -- reading the
# upload off the socket is inside the budget. A ~1 MB document on a slow link
# can spend most of a minute in transfer before add_doc() runs at all, which is
# why the old 60 s produced "[CRITICAL] WORKER TIMEOUT" on large POSTs.
AVER_GUNICORN_TIMEOUT=${AVER_GUNICORN_TIMEOUT:-300}
# Threads per worker for the gthread worker class (see -k below). This is
# gunicorn concurrency, NOT the same knob as AVER_WEB_THREADS, which sets
# OMP_NUM_THREADS for the embedding model above.
AVER_GUNICORN_THREADS=${AVER_GUNICORN_THREADS:-4}

# -k gthread: the default `sync` worker writes its arbiter heartbeat only at the
# top of the accept loop, so a request blocked reading a slow upload looks like a
# hung process and gets SIGABRT'd at --timeout. gthread keeps the accept loop
# (and therefore the heartbeat) running while requests execute in a thread pool,
# so slow clients no longer kill the worker.
exec ./venv/bin/gunicorn \
	-w "$AVER_WEB_CONCURRENCY" \
	-k gthread \
	--threads "$AVER_GUNICORN_THREADS" \
	--timeout "$AVER_GUNICORN_TIMEOUT" \
	-b "0.0.0.0:$AVER_PORT" \
	bottleAPI:app
