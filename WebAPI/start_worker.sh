#!/bin/sh

# Inference worker launcher. Owns the CPU: one process, all cores to one job.

# Load local deployment config if present (see .env.example).
ENV_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$ENV_DIR/.env" ]; then
	set -a
	. "$ENV_DIR/.env"
	set +a
fi

export HF_HOME=./venv/huggingface

# Give the model all cores (2 physical / 3 vCPU on the production VM).
# Tune with scripts/benchmark_batch_size.py if needed.
export OMP_NUM_THREADS=${AVER_WORKER_THREADS:-3}
export MKL_NUM_THREADS=${AVER_WORKER_THREADS:-3}
export OPENBLAS_NUM_THREADS=${AVER_WORKER_THREADS:-3}

# GPU usage (set per machine):
#   unset (default) -> auto: use a GPU when CUDA is available, else CPU
#   AVER_USE_GPU=1  -> use a GPU (falls back to CPU if none/CUDA unavailable)
#   AVER_USE_GPU=0  -> force CPU only
# A GPU is acquired per job and released while idle. Tune the picker with
# AVER_GPU_MIN_FREE_GB (default 14), AVER_GPU_WHOLE_NODE (default 1),
# AVER_GPU_WAIT_SECONDS (default 600). Pass AVER_USE_GPU through if it is set.
[ -n "${AVER_USE_GPU+x}" ] && export AVER_USE_GPU

exec ./venv/bin/python worker.py
