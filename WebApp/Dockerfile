FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Pin stanza's model cache to a fixed location inside the image. Same env
# var is read by `core.utils.pos_method`, so local dev / CI can point
# elsewhere via STANZA_RESOURCES_DIR without touching code.
ENV STANZA_RESOURCES_DIR=/app/.stanza_resources

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Download the latest installer
ADD https://astral.sh/uv/install.sh /uv-installer.sh

# Run the installer then remove it
RUN sh /uv-installer.sh && rm /uv-installer.sh

# Ensure the installed binary is on the `PATH`
ENV PATH="/root/.local/bin/:$PATH"

# Copy only dependency files first for better caching
COPY pyproject.toml uv.lock requirements.txt ./

# Install dependencies (cached unless these files change)
RUN uv sync --locked

# Copy the rest of the application code
COPY . .

# Install dotenvx
RUN curl -sfS https://dotenvx.sh/install.sh | sh

# Pre-download stanza POS models for the languages we tag at MUNI-API
# ingestion time. Without these the granular selection_method falls back
# to bucket-level averages (still correct, just less precise). The runtime
# auto-downloader (pos_method.ensure_model) is the safety net if anyone
# ever runs this image with a fresh cache volume mounted over /app/.stanza_resources.
# Called via the helper rather than `manage.py fetch_pos_models` because
# Django settings (SECRET_KEY etc.) aren't available at build time.
RUN uv run python -c "from core.utils.pos_method import SUPPORTED_LANGUAGES, ensure_model; assert all(ensure_model(l) for l in sorted(SUPPORTED_LANGUAGES)), 'stanza model download failed'"