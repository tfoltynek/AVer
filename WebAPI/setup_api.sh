#!/bin/sh

set -e
set -x

# Load local deployment config if present (see .env.example) — notably
# AVER_USE_GPU, which selects the CPU vs CUDA torch build below.
ENV_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$ENV_DIR/.env" ]; then
	set -a
	. "$ENV_DIR/.env"
	set +a
fi

# Dependencies are managed with uv (pyproject.toml + uv.lock).
# Install uv if you don't have it: https://docs.astral.sh/uv/getting-started/installation/
#   curl -LsSf https://astral.sh/uv/install.sh | sh
if ! command -v uv >/dev/null 2>&1; then
	echo "ERROR: 'uv' not found. Install it with:"
	echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
	exit 1
fi

# Create the environment as ./venv (so existing scripts and systemd units keep
# their ./venv/bin/... paths) and install the exact locked dependency set.
# torch comes from the CPU-only PyTorch index, configured in pyproject.toml.
export UV_PROJECT_ENVIRONMENT=venv
uv sync --frozen

# Torch build selection. The locked deps install the CPU-only torch (right for
# the CPU VM). On a GPU machine set AVER_USE_GPU=1 to overwrite torch with a
# CUDA build so the worker can run on a GPU (see gpu.py / start_worker.sh). The
# CUDA channel and version are overridable via AVER_TORCH_CUDA / AVER_TORCH_VERSION.
case "$(printf '%s' "${AVER_USE_GPU:-}" | tr '[:upper:]' '[:lower:]')" in
	1|true|yes|on)
		TORCH_VERSION="${AVER_TORCH_VERSION:-2.4.1}"
		TORCH_CUDA="${AVER_TORCH_CUDA:-cu121}"
		echo "AVER_USE_GPU=1: installing CUDA torch ${TORCH_VERSION} (${TORCH_CUDA})"
		# --reinstall-package torch is required: uv sync above installs the
		# CPU build (2.4.1+cpu), which already "satisfies" 2.4.1, so without
		# forcing a reinstall uv would skip the CUDA wheel and leave CPU torch.
		uv pip install --reinstall-package torch --python venv/bin/python \
			--index-url "https://download.pytorch.org/whl/${TORCH_CUDA}" \
			"torch==${TORCH_VERSION}"
		./venv/bin/python -c "import torch; print('torch', torch.__version__, 'cuda_available', torch.cuda.is_available())"
		;;
	*)
		echo "CPU-only torch (set AVER_USE_GPU=1 before setup to install a CUDA build)."
		;;
esac

echo "Downloading NLTK data"
./venv/bin/python -m nltk.downloader -d ./venv/nltk_data \
		  punkt averaged_perceptron_tagger averaged_perceptron_tagger_eng universal_tagset punkt_tab

ln -sf ../MLmethod/ .
ln -sf ../MLmethod/MT5_model_method.py .

echo "Virtual environment created"
