# AVer
App that generates a test from a document you wrote yourself. Fill in the missing words and see how well the answers match your own writing.

Documentation: [calibration and reproduction (cs)](docs/calibration.md)

Licence: MIT, see [LICENSE](../LICENSE). Third-party files shipped with the
app are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Tech Stack

- **Python 3.12**
- **Django 6.0**
- **Celery 5.6** (background tasks)
- **Redis** (message broker for Celery, via Docker)
- **uv** (fast Python package/dependency manager)
- **dotenvx** (environment variable management)
- **Docker** (for local services)
- **spaCy** (NLP)
- **stanza** (POS tagging at ingestion, the fallback for picking a word's pA/pN when the NLP service reports no selection category we recognise)
- **Other**: django-cors-headers, django-debug-toolbar, gunicorn, lingua-language-detector, lucide, magika, pymupdf, python-docx, etc.

## Local Development

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (install via `pipx install uv`)
- [dotenvx](https://dotenvx.sh/)
- Docker & Docker Compose

### 0. Create the environment file

```sh
cp .env.example .env.dev
```

Set `SECRET_KEY` to a long random string. `.env.dev` is ignored by git.

### 1. Install Python dependencies

```sh
uv sync --dev
```

### 1b. Fetch POS models (one-time, ~700 MB)

A word's calibration bucket normally comes from the selection category the
MUNI service reports with it. Stanza tokenize+pos models cover the case
where that category is missing or is one we do not map, so the word still
gets a granular Hitzinger pA/pN instead of the fallback pair. They live
under `$STANZA_RESOURCES_DIR` (or `~/stanza_resources/` if unset) and are
fetched once.

```sh
dotenvx run -f .env.dev -- uv run ./manage.py fetch_pos_models
```

The runtime auto-downloads on first use too (so this step is optional
locally), but pre-warming avoids a ~10 s delay on the first AI analysis.
Docker bakes them into the image at build time.

### 2. Start Redis (required for Celery)

```sh
docker compose -f docker-compose.dev.yml up -d redis
```

### 3. Run Django development server

```sh
dotenvx run -f .env.dev -- uv run ./manage.py runserver
```

### 4. Run Celery worker

```sh
dotenvx run -f .env.dev -- uv run celery -A aver worker -l INFO
```

### 5. (Optional) Run Django shell

```sh
dotenvx run -f .env.dev -- uv run ./manage.py shell
```

## Environment Variables

All development variables are in `.env.dev`, created from `.env.example`. Key settings:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `CORS_ALLOWED_ORIGINS`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`

## Useful Commands

- **Collect static files:**  
  `dotenvx run -f .env.dev -- uv run ./manage.py collectstatic`
- **Run tests:**  
  `uv run pytest tests/unit` (fast, what CI runs) and
  `dotenvx run -f .env.dev -- uv run pytest tests/e2e` (Playwright browsers, run locally before pushing)
- **Lint:**  
  `uv run ruff check .`
- **Format HTML (djlint):**  
  `uv run djlint . --reformat`

## Pre-commit hooks (optional but recommended)

Hooks for ruff + djlint + basic hygiene are configured in `.pre-commit-config.yaml`.
Enable them once per clone:

```sh
uv tool install pre-commit   # or: pipx install pre-commit
pre-commit install           # writes the git hook
pre-commit run --all-files   # one-time sweep of the codebase
```

## Production

- Use `docker-compose.yml` for deployment.
- Set up a production-ready `.env` file (same keys as `.env.example`, `DEBUG=False`).

## Not included in this repository

Only material that can be distributed under the MIT License is published
here. A deployment that should look like the public instance needs these
files added at the paths the code already expects:

| What | Expected path | Without it |
|---|---|---|
| User-study dataset (CSV) | `core/static/core/data/aver-user-studies-responses.csv` | The download link on `/data` returns 404; its e2e test is skipped. |
| Partner logos (TA ČR, PEF MENDELU, FSV UK, FI MUNI) | `core/static/core/images/logos/` (`tacr.png`, `pef-mendelu-{light,dark}.webp`, `fsv-uk-{light,dark}.svg`, `fi-muni-{light,dark}.webp`) | The footer shows the partner names as text. |
| Demo texts under copyright or other licences | `test_documents/data/<name>_request.json` + `_response.json`, cover in `core/static/core/images/covers/<name>.png`, entry in `test_documents/metadata.json` | Try mode offers the 11 public-domain and official texts shipped here. |
