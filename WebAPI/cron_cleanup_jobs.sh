#!/bin/sh

# Purge expired jobs from the SQLite store. Add to crontab, e.g. hourly:
#   0 * * * * /nlp/projekty/aver/xspiege1/AVer/WebAPI/cron_cleanup_jobs.sh >> /path/to/cleanup.log 2>&1
#
# (The worker also purges expired jobs while idle, so this cron is a backup.)

cd "$(dirname "$0")" || exit 1
export AVER_DB_PATH=${AVER_DB_PATH:-./jobs.db}
exec ./venv/bin/python job_store.py cleanup
