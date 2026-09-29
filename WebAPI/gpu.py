"""
Dynamic CUDA GPU selection for the AVer inference worker.

The worker keeps the MT5 model on the CPU while idle and moves it onto a GPU
only for the duration of a job, releasing the device again afterwards. This
module picks which GPU to use, based on live `nvidia-smi` readings:

  * a candidate GPU must have at least AVER_GPU_MIN_FREE_GB free (default 14),
  * a fully-idle GPU (no other process using it) is strongly preferred, so we
    take a whole node rather than crowding onto one somebody else is using,
  * if nothing is free we wait AVER_GPU_WAIT_SECONDS (default 600 = 10 min) and
    try again, indefinitely.

Configuration (all optional):
  AVER_USE_GPU            "1"/"true" to enable (default: enabled iff CUDA is
                          available to torch). Set to "0" to force CPU.
  AVER_GPU_MIN_FREE_GB    minimum free memory a GPU must have (default 14).
  AVER_GPU_WHOLE_NODE     "1" (default) prefer a fully-idle GPU; "0" allows any
                          GPU meeting the free-memory bar.
  AVER_GPU_WAIT_SECONDS   seconds to wait before retrying when none is free
                          (default 600).
  CUDA_VISIBLE_DEVICES    honored: only GPUs visible to this process are picked.

nvidia-smi reports indices in the physical numbering. When CUDA_VISIBLE_DEVICES
is set, torch re-indexes the visible GPUs as 0..N-1; we map back so the device
string we hand to torch is correct.
"""

import logging
import os
import subprocess
import time

logger = logging.getLogger("aver.gpu")


def _env_flag(name, default):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# 14 (not 15) so the smaller ~14.9 GB-free cards still qualify.
MIN_FREE_GB = float(os.getenv("AVER_GPU_MIN_FREE_GB", "14"))
PREFER_WHOLE_NODE = _env_flag("AVER_GPU_WHOLE_NODE", True)
WAIT_SECONDS = float(os.getenv("AVER_GPU_WAIT_SECONDS", "600"))
# A GPU with less than this much used memory counts as "idle" (a whole free
# node). Small allocations from monitoring tools shouldn't disqualify it.
IDLE_USED_MIB = float(os.getenv("AVER_GPU_IDLE_USED_MIB", "300"))


def cuda_available():
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:  # noqa: BLE001 - torch missing or broken => no CUDA
        return False


def gpu_enabled():
    """Whether the worker should try to use a GPU at all.

    Controlled by AVER_USE_GPU:
        unset / empty -> auto: use a GPU iff CUDA is available
        0/false/no    -> force CPU only
        1/true/yes    -> use a GPU (still CPU if CUDA is unavailable)
    """
    raw = os.getenv("AVER_USE_GPU")
    if raw is not None and raw.strip() != "":
        if not _env_flag("AVER_USE_GPU", False):
            return False  # explicitly disabled
        if not cuda_available():
            logger.warning("AVER_USE_GPU is set but CUDA is unavailable; running on CPU.")
            return False
        return True
    return cuda_available()  # auto


def _visible_indices():
    """Physical GPU indices visible to this process, in torch's order.

    Returns None when CUDA_VISIBLE_DEVICES is unset (all GPUs visible).
    """
    raw = os.getenv("CUDA_VISIBLE_DEVICES")
    if raw is None or raw.strip() == "":
        return None
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _query_gpus():
    """Return [(physical_index, free_mib, used_mib, util_pct), ...] from nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,memory.free,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning("nvidia-smi query failed (%s); cannot select a GPU.", exc)
        return []

    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            free_mib = float(parts[1])
            used_mib = float(parts[2])
            util = float(parts[3])
        except ValueError:
            continue
        gpus.append((idx, free_mib, used_mib, util))
    return gpus


def _torch_device_for(physical_index, visible):
    """Map a physical GPU index to the cuda:N string torch expects."""
    if visible is None:
        return f"cuda:{physical_index}"
    # torch sees visible GPUs renumbered 0..len(visible)-1, in list order.
    return f"cuda:{visible.index(physical_index)}"


def _pick(min_free_gb=None):
    """Pick one GPU now, or return None if none meets the bar.

    Returns (torch_device_str, physical_index) or None.
    """
    min_free_mib = (MIN_FREE_GB if min_free_gb is None else min_free_gb) * 1024.0
    visible = _visible_indices()
    gpus = _query_gpus()
    if visible is not None:
        visible_set = set(visible)
        gpus = [g for g in gpus if g[0] in visible_set]

    eligible = [g for g in gpus if g[1] >= min_free_mib]
    if not eligible:
        return None

    idle = [g for g in eligible if g[2] <= IDLE_USED_MIB]
    if PREFER_WHOLE_NODE and idle:
        # Among fully-idle GPUs, take the smallest one that still fits, leaving
        # the big cards free for jobs that actually need them.
        idx, free_mib, used_mib, _ = min(idle, key=lambda g: g[1])
        logger.info("Selected idle GPU %d (%.0f MiB free).", idx, free_mib)
        return _torch_device_for(idx, visible), idx

    # No whole node available: take the GPU with the most free memory.
    idx, free_mib, used_mib, _ = max(eligible, key=lambda g: g[1])
    logger.info("Selected shared GPU %d (%.0f MiB free, %.0f MiB used).",
                idx, free_mib, used_mib)
    return _torch_device_for(idx, visible), idx


def acquire(min_free_gb=None, should_continue=None):
    """Block until a suitable GPU is free, then return its torch device string.

    Retries every WAIT_SECONDS while none is available. `should_continue`, if
    given, is a no-arg callable polled while waiting; returning False aborts the
    wait and this returns None (used so a shutdown signal can interrupt it).
    """
    while True:
        picked = _pick(min_free_gb)
        if picked is not None:
            return picked[0]
        logger.info("No GPU with >= %.0f GB free; waiting %.0f s before retry.",
                    MIN_FREE_GB if min_free_gb is None else min_free_gb, WAIT_SECONDS)
        waited = 0.0
        step = min(5.0, WAIT_SECONDS)
        while waited < WAIT_SECONDS:
            if should_continue is not None and not should_continue():
                logger.info("GPU wait interrupted; staying on CPU.")
                return None
            time.sleep(step)
            waited += step


def release():
    """Free cached GPU memory so the device is available to other jobs."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
