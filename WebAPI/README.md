# AVer

AVer takes a document (paragraphs of running prose in `en`, `cs`, or `sk`),
selects a handful of words or phrases that a language model expects to be able
to fill back in, and returns those choices with the original sentence blanked
out. The result is used to generate cloze-style comprehension questions. Analysis
runs on the CPU with an INT8-quantized `mt5-large` (or on a CUDA GPU), takes
minutes to hours per document, and is served through an **asynchronous** HTTP
API.

This document is both the technical reference for the pipeline and the
operations manual for the deployment. It is split into:

- [Overview](#overview) — what the service does, the end-to-end diagram, and
  where the code lives.
- [Quick start](#quick-start) — the smallest useful submit-and-poll example.
- [Concepts and pipeline](#concepts-and-pipeline) — every stage from raw text
  to returned words, in order.
- [API reference](#api-reference) — endpoints, request and response shapes,
  status codes.
- [Configuration snapshot](#configuration-snapshot) — the current production
  values (cloze classes, LM inference, filters, thresholds) with pointers to
  their source files.
- [Deployment](#deployment) — how to bring the service up on a new host.
- [Operations](#operations) — day-2 topics (updates, monitoring, OOM, DB
  inspection, ...).
- [Development](#development) — repo layout and how to run the tests.

## Overview

The service exposes a small REST API. `POST /document` enqueues a job and
returns a `job_id` immediately; the client polls `GET /jobs/<id>` and fetches
`GET /jobs/<id>/result` once the state flips to `finished`. A separate
synchronous endpoint, `POST /similarity`, judges whether two texts match.

Two processes cooperate through a shared SQLite file:

- **`aver-backend`** — gunicorn + Bottle. Thin: enqueues jobs, serves status
  and results, hosts the sentence-similarity model. Started by
  `WebAPI/start_api.sh`.
- **`aver-worker`** — a single inference process that holds MT5 and processes
  one job at a time, using all CPU cores. Started by `WebAPI/start_worker.sh`.

Both point at the same `AVER_DB_PATH` (default `WebAPI/jobs.db`, WAL mode).
Results are retained for `AVER_JOB_TTL_SECONDS` (default 7 days), then purged
by the worker while idle and/or by `WebAPI/cron_cleanup_jobs.sh`.

```
                            HTTP + X-API-Key
        client  ─────────────────────────────────►  aver-backend  (gunicorn + Bottle)
          ▲                                            │
          │  GET /jobs/<id>[/result]                   │  INSERT job
          │                                            ▼
          └────── read result ────────────────────  jobs.db  (SQLite, WAL)
                                                       ▲
                                                       │  CLAIM oldest queued
                                                       │  UPDATE progress
                                                       │  WRITE result
                                                       │
                                                  aver-worker  (single process)
                                                       │
                          ┌──────────────────┬─────────┴──────────┬───────────────┐
                          ▼                  ▼                    ▼               ▼
                    filter sentences    detect lang / POS    extract n-grams   LM verify
                    (boilerplate,       (en/cs/sk +          per PICK_CLASSES  (MT5-large
                     length, refs)      stopwords)                               INT8)
                                                                                    │
                                                                            ┌───────┴───────┐
                                                                            ▼               ▼
                                                                       accept (top-K)   random fallback
                                                                            │               │
                                                                            └───────┬───────┘
                                                                                    ▼
                                                                            result payload
                                                                            (words[])
```

**Where the code lives** (see [Repo layout](#repo-layout) for the full list):

- `WebAPI/bottleAPI.py` — the web tier.
- `WebAPI/worker.py` — the inference worker main loop.
- `WebAPI/job_store.py` — the SQLite schema and every DB access.
- `WebAPI/cloze_config.py` — cloze-class rules and quotas (`PICK_CLASSES`,
  `CLASSES_NUM`).
- `MLmethod/MT5_model_method.py` — the analyzer: sentence filter, POS tagging,
  n-gram extraction, LM verification, random fallback.
- `MLmethod/language_detection.py` — the `{en, cs, sk}` detector used by
  `AVER_LANGUAGE_AUTO_CORRECT`.
- `WebAPI/similarity.py` — the sentence-similarity backend for `/similarity`.
- `WebAPI/aver_db.py` — stdlib-only CLI for inspecting `jobs.db`.

## Quick start

Submit a document, poll for it, and fetch the result. Set `KEY` to your
`AVER_API_KEY` (omit the header if authentication is disabled):

```sh
KEY="your-api-key"

# 1. Submit -> HTTP 202, body carries {job_id, status_url, expires_at}
JOB=$(curl -s -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d @WebAPI/sample_requests/test_request.json http://localhost:11122/document \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# 2. Poll -> {state, progress, timestamps, error}
curl -s -H "X-API-Key: $KEY" http://localhost:11122/jobs/$JOB

# 3. Fetch once state == "finished" -> {document_id, words: [...]}
curl -s -H "X-API-Key: $KEY" http://localhost:11122/jobs/$JOB/result
```

There is a synchronous endpoint too, `POST /similarity`, for scoring whether
two texts match. It never uses the job queue and always returns HTTP 200; see
[POST /similarity](#post-similarity) for the shape.

## Concepts and pipeline

### The asynchronous job queue

Document analysis on CPU can take **hours** for large inputs, far longer than
any HTTP timeout. The API is therefore asynchronous: `POST /document`
enqueues a job and returns immediately with a `job_id`; the client polls for
status and downloads the result when finished.

Job states cycle through `queued` → `processing` → `finished` (or `failed`,
or `cancelled`). Notes on behaviour:

- **No automatic retry.** If the worker is restarted mid-job that job is
  marked `failed` with `"worker restarted during processing; please
  resubmit"`; the client resubmits if desired. See
  [Painless updates](#painless-updates) for how to restart safely.
- Expired jobs return `404` (no tombstone is kept).
- `AVER_MAX_QUEUE` (default `0` = unbounded) caps the backlog; over it,
  `POST` returns `429` with a `Retry-After`.
- Bottle's `MEMFILE_MAX` is 10 MiB (set in `WebAPI/bottleAPI.py`). Bigger
  bodies are rejected before the handler sees them. An upstream proxy may
  impose its own upload limit.

### Pipeline stages

Every job goes through six stages, in order. The stage is executed by the
worker; the web tier only enqueues and serves the result.

#### 1. Sentence extraction and filtering

Before the analyzer creates any candidates, it applies a deterministic
text-quality filter in `MLmethod/MT5_model_method.py`. The goal is to avoid
spending LM inference on references, document structure, and text that is
unlikely to make a useful question. The filter preserves the original
paragraph and sentence indexes of every remaining candidate.

1. **Reference sections.** Paragraphs are processed in order. An exact
   bibliography heading (`References`, `Bibliography`, `Works Cited`,
   `Zdroje`, `Literatura`, `Referencie`, and supported Czech/Slovak variants)
   and every following paragraph are excluded.
2. **Structural hard rejects.** Individual sentences are excluded when they
   contain a URL, email address, DOI/ISBN/ISSN/URN/arXiv identifier; consist
   only of a citation such as `[12]` or `(Smith, 2020)`; are
   figure/table/code captions; are table-of-contents rows; or contain more
   than 40 % non-letter visible characters.
3. **Conservative weak-text score.** Remaining sentences receive one point
   for each of: fewer than 8 alphabetic tokens, fewer than 3 valid content
   tokens (`NOUN`, `VERB`, `ADJ`, `ADV`), a content-token ratio below 35 %,
   and a list/heading-like form. Only sentences with 3 or more points are
   excluded, so short but informative prose remains eligible.
4. **Citation spans.** Long double-quoted passages and parenthetical/bracketed
   citations are retained as context, but candidates overlapping their spans
   cannot be used as blanks. For example, the explanation around
   `(Smith, 2020)` remains eligible, while the citation itself cannot become
   a question.

The worker logs aggregate rejection counts by reason, never the rejected
source text. These patterns are intentionally conservative and are static
configuration in the analyzer; update the regression tests in
`tests/unit/test_analysis.py` when adding a new accepted or rejected document
pattern.

#### 2. Language auto-detection

A wrong `language` in the request silently activates the wrong POS tagger
and the wrong stopword list. That is what let function words leak into
recent bigram / trigram candidates. `MLmethod/language_detection.py` guards
against it with a dependency-free detector for `{en, cs, sk}`.

- **Off by default.** The feature is opt-in via
  `AVER_LANGUAGE_AUTO_CORRECT=1`. With the flag off the analyzer trusts the
  request's `language` verbatim, matching the historical behavior.
- **On:** the worker joins all paragraphs (they always share a language in
  practice), scores each language, and — on disagreement — swaps `language`
  for the rest of the pipeline. A `WARNING` line lands in the worker
  journal, and the verdict is written into the response payload under
  `language_correction` (`declared`, `detected`, `applied`, `scores`,
  `margin`, `n_tokens`, `n_chars`).

The detector combines two features:

1. Per-language stopword hit rate against the shipped stopword lists.
2. Rate of Slavic-exclusive letters: `ě ř ů` for Czech and `ľ ĺ ŕ ô ä` for
   Slovak. English is *penalized* by either, so an English document with a
   few coincidentally-Czech function words cannot beat real Czech text.

The detector abstains on very short input (`< 8` alphabetic tokens or
`< 40` alphabetic characters) and returns the declared language, so
borderline inputs never flip to a surprise language.

Inspect a stored job's verdict with `aver_db.py show <id>` (see
[Inspecting the job DB](#inspecting-the-job-db-aver_dbpy)); it prints the
correction block whenever the response carries one.

#### 3. Candidate extraction (cloze classes)

The analyzer POS-tags every kept sentence, then walks it token by token and
emits every n-gram that matches a **cloze class**. Which n-grams count is
configured by two paired dicts in `WebAPI/cloze_config.py`:

- **`PICK_CLASSES`** — what to extract per class:
  - `pos_tags` — the POS tags the class targets. Uses the Universal POS
    tagset (`NOUN`, `ADJ`, `ADV`, `VERB`, `PRON`, `NUM`, ...).
  - `num_of_words` — n-gram length (1 = unigram, 2 = bigram, 3 = trigram).
  - `match` — how `pos_tags` is matched (optional, default `"all"`):
    - `"all"` — **every** token's tag must be in `pos_tags` (whitelist). For
      example `["NOUN","ADV","ADJ"]` keeps n-grams made only of nouns,
      adverbs, and adjectives.
    - `"contains"` — `pos_tags` must be a **subset** of the n-gram's tags,
      i.e. the n-gram must contain every listed tag at least once. For
      example `["ADJ"]` with `num_of_words: 3` keeps trigrams that contain
      an adjective.
  - `allowed_pos_tags` — optional secondary whitelist. When set, **every**
    tag in the n-gram must also be in this list. Used with
    `match: "contains"` to restrict the "other" tokens in a trigram (for
    example, keep an adjective trigram, but only if the other two tokens
    are `VERB|PRON|NUM|ADJ|NOUN`).
  - `LM_verify` — whether to run the MT5 plausibility check on the class.
    `True` (default) sends every candidate through the model and keeps only
    those the model ranks within `[MIN_RANGE, MAX_RANGE)`. `False` skips
    the check and keeps the most-frequent candidates directly. All classes
    currently run with `LM_verify: True`.
  - In both `match` modes every token must also be a valid word (alphabetic,
    non-stopword), so n-grams with punctuation or stopwords are skipped.
- **`CLASSES_NUM`** — how many words to return per class. A class is only
  used if it appears here; the keys must match `PICK_CLASSES` exactly (a
  key present in one but not the other raises `KeyError`).

Candidates are extracted, ranked, and selected **per class independently**
(each class has its own list; frequency ranking compares trigrams only
against trigrams, and so on).

For the actual production values (which classes, which quotas, which POS
targets), see [Cloze classes and quotas](#cloze-classes-and-quotas).

#### 4. LM verification

For every class with `LM_verify: True` the analyzer runs the candidates
through MT5. It builds the sentence with the candidate blanked out
(`<extra_id_0>`), asks the model for the top-K completions in beam-search
mode, and accepts the candidate iff the model's rank of the original word
falls in `[AVER_MIN_RANGE, AVER_MAX_RANGE)`. The default `[1, 41)`
deliberately skips rank 0 (the model's single top prediction) — a word the
model already puts at the very top is too predictable to make a useful
cloze — and caps the accept range at rank 40 (out of the 41 predictions
the beam returns). See
[LM inference parameters](#lm-inference-parameters) for the values.

The verification runs in batches (`AVER_BATCH_SIZE` candidates per
forward pass). This is the memory-heavy step: batches dominate the
transient RSS peak.

The exact hyperparameters (beam width, top-K, batch size, position window)
are listed in [LM inference parameters](#lm-inference-parameters).

#### 5. Random fallback

Sometimes the analyzer cannot fill every quota — the paper is short, or the
LM rejects every noun candidate the extractor produced. When that happens
the worker fills the missing slots by picking **random** candidates from
the leftover pool. Words picked this way:

- get `"category": "random"` in the response;
- keep the original `PICK_CLASSES` key they would have belonged to under
  `"origin_category"`, so the frontend can still tell "this was going to
  be a `unigrams_NOUN`";
- have `"predictions": null` and `ml_pos = -1` — they were **not** verified
  by the LM;
- appear only when at least one slot triggered the fallback; otherwise the
  category is absent from the response entirely.

Random fallback is opt-out through under-filling `CLASSES_NUM`: reduce a
class's quota and no random word will ever be issued for it.

#### 6. Selection and result assembly

For each class the analyzer keeps up to `CLASSES_NUM[class]` accepted
candidates. If a class cannot reach its quota, the shortfall is filled in
this order:

1. **Plausible words from other classes** that were found but not needed
   there (reused for free — no extra inference).
2. If still short, the model is run on **leftover candidates from other
   classes** to find more plausible words.
3. Only as a **last resort**, remaining slots are filled with **random**
   candidates (Stage 5 above).

So random fill happens only when plausible candidates are genuinely
exhausted, and a word borrowed to cover another class's shortfall still
reports its own `category`.

The final result is assembled into `{document_id, words: [...]}` and
written into the job row. `words[i].category` reflects the class the word
was extracted from (or the literal `"random"` for fallback words). See
[GET /jobs/{id}/result](#get-jobs-id-result) for the payload shape.

### Sentence similarity endpoint

`POST /similarity` is separate from the pipeline above: it does not use the
job queue and does not call MT5. It judges whether two texts match by
cosine similarity against a threshold, and returns the verdict in a grading
schema. It uses `sentence-transformers` (multilingual, en/cs/sk), is fast
and synchronous, and **always responds with HTTP 200** — a malformed
request is reported as `result: "fail"`, not an HTTP error.

The embedding model loads lazily on first request in the web process;
configure it with `AVER_SIMILARITY_MODEL` (default
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`) and the match
cutoff with `AVER_SIMILARITY_THRESHOLD` (default `0.7172`). See
[POST /similarity](#post-similarity) for the payload shape and behaviour.

## API reference

### Authentication

All endpoints require an API key when `AVER_API_KEY` is set: send it in the
`X-API-Key` header. If `AVER_API_KEY` is unset, the check is disabled. The
one exception is `GET /health`, which is always public so that external
uptime monitors can poll it without a shared secret.

### Endpoint index

| Method & path | Purpose |
|---|---|
| `POST /document` | Submit a document. Returns `202` with `{job_id, status_url, expires_at}`. |
| `GET /jobs/<id>` | Job status: `state`, `progress`, timestamps, `error`. `404` if unknown or expired. |
| `GET /jobs/<id>/result` | The analysis result (`200`) when finished; `409` (+`Retry-After`) while pending; `200` with `{state:"failed", error}` on failure or `{state:"cancelled"}` after cancellation; `404` if unknown/expired. |
| `POST /jobs/<id>/cancel` | Cancel a queued or processing job. Cancellation is immediate for queued jobs and cooperative between worker batches for processing jobs. |
| `POST /similarity` | Cosine similarity of sentences (synchronous). |
| `GET /health` | Public service-health snapshot for uptime monitors. `200` when healthy, `503` when degraded. |

### POST /document

Submits a document for analysis. Returns HTTP `202` immediately with the
job identifier.

`POST /document` only validates that the JSON body is present and contains
`language`, `document_id`, and `paragraphs`. Invalid nested values can
therefore be accepted as a job and fail later during analysis. Clients
should retain the original request body until they have received a
finished result.

### GET /jobs/{id}

Returns the current job state. The response's `progress` object has four
integer fields:

```json
"progress": { "done": 12, "total": 31, "accepted": 9, "total_accepted": 15 }
```

- `done` / `total` — count **LM inference batches**, not returned words.
  One batch runs `AVER_BATCH_SIZE` candidates through MT5 (default 5).
  `total` is an upper bound: the analyzer stops early once every class hits
  its quota, so `done` can plateau below `total` before the state flips to
  `finished`. This makes `done / total` a reasonable ETA proxy but *not* a
  "how many words we have so far" number.
- `accepted` / `total_accepted` — the number of candidates the language
  model has already **accepted** for output, and the target the job is
  aiming for (that is, the current `sum(CLASSES_NUM.values())`, default
  **15**, or whatever `AVER_CLOZE_TOTAL` / `AVER_CLASSES_NUM` sets). Both
  are stable per job: `total_accepted` is captured at enqueue time and does
  not change even if the env is updated mid-flight. Random-fill fallback
  candidates (Stage 5) are **not** counted in `accepted` — see
  [Random fallback](#5-random-fallback) — so `accepted` measures "real,
  LM-verified accepts". The final finished result may return more words
  than `accepted` when random fallback filled the shortfall; conversely,
  `accepted` cannot exceed `total_accepted`. `total_accepted` is `null`
  only on rows migrated in before this column existed.

Typical trajectory of a healthy run: `done` climbs, `total` stays roughly
constant (may grow slightly as more sentences are added to the pool),
`accepted` climbs toward `total_accepted` and levels off; when
`accepted == total_accepted` the analyzer usually stops before
`done == total`.

### GET /jobs/{id}/result

Returns the analysis output once the job is `finished`.

Result shape: `{ "document_id", "words": [...] }`. Each item in `words`
has:

- `paragraph_id`, `sentence_index`, `index` — location of the word.
- `content` — the picked word or phrase; `sentence_blanked` — its sentence
  with the word replaced by `<<BLANK>>`.
- `predictions` — the model's ranked guesses with scores (`null` for words
  that were filled in randomly rather than chosen by the model).
- `pos_tags` — the space-separated Universal POS tags for each token of the
  n-gram, in surface order.
- `category` — the cloze class that produced the word, i.e. a
  `PICK_CLASSES` key such as `"unigrams_ADJ"` or `"trigrams_w_ADJ"`. Lets
  the frontend show which method/category each word came from. A word may
  be used to fill another category's shortfall, but `category` always
  reflects the class it was extracted from. The literal string `"random"`
  marks a random fallback word; see [Random fallback](#5-random-fallback).
- `origin_category` — present **only** for random fallback words; the class
  the candidate would have belonged to if the LM had accepted it.
- `additional_info` — reserved (currently empty).

The response optionally carries a top-level `language_correction` block if
[Language auto-detection](#2-language-auto-detection) was enabled and the
detector overruled the declared language.

### POST /jobs/{id}/cancel

Cancel a queued or processing job. Cancellation is immediate for queued
jobs and cooperative between worker batches for processing jobs — the
worker checks after each LM batch and stops there.

### POST /similarity

Cosine similarity of sentences, synchronous. Uses `sentence-transformers`;
see [Sentence similarity endpoint](#sentence-similarity-endpoint) for the
concept. `result` is `"yes"` when similarity ≥ `AVER_SIMILARITY_THRESHOLD`,
`"no"` otherwise, or `"fail"` for a bad request. The schema also allows
`partial`/`error`, which this endpoint does not emit. A single pair returns
one object; a `pairs` batch returns a JSON **array** of objects, one per
pair in order.

```sh
# single pair
curl -s -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d @WebAPI/sample_requests/test_similarity_request.json http://localhost:11122/similarity
# -> {"result": "yes", "text": "Answer matches."}

# batch (array out, one per pair; a pair missing text1/text2 -> result "fail")
curl -s -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"pairs":[{"text1":"a","text2":"b"},{"text1":"c","text2":"d"}]}' \
  http://localhost:11122/similarity
# -> [{"result": "no", "text": "..."}, {"result": "no", "text": "..."}]
```

Operational notes for the default model
(`paraphrase-multilingual-MiniLM-L12-v2`):

- **Memory / speed.** It is ~0.5 GB resident and fast per request on CPU.
  The heavier `paraphrase-multilingual-mpnet-base-v2` (~1.1 GB, more
  accurate) is available by setting `AVER_SIMILARITY_MODEL` to it.
- **Loads in the web process.** The model is held by `aver-backend`, so its
  first `/similarity` request pays the load cost and briefly competes with
  the worker for CPU. To avoid a slow first request, warm it up after
  start, e.g.:

  ```sh
  curl -s -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"text1":"warm up","text2":"warm up"}' http://localhost:11122/similarity
  ```

- **Download on first use.** The weights are fetched from the Hugging Face
  hub the first time the model loads. On an offline VM, pre-download them
  during setup (or point `AVER_SIMILARITY_MODEL` at a local directory), for
  example:

  ```sh
  ./venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"
  ```

### GET /health

Public service-health snapshot. Meant for uptime monitors (UptimeRobot,
StatusCake, ...); no `X-API-Key` required. Returns `200` with
`"status":"ok"` when every probe passes, `503` with `"status":"degraded"`
when at least one probe raised a concern. The full body ships in both
cases so an operator can see *why* the endpoint degraded. See
[Health endpoint and uptime monitoring](#health-endpoint-and-uptime-monitoring)
for the full payload, the monitoring recipe, and the tunable thresholds.

### Error handling and retry policy

| Response or state | Meaning | Client action | Recoverability |
|---|---|---|---|
| `401 Unauthorized` | Missing or invalid `X-API-Key`. | Supply valid credentials, then repeat the request. | Recoverable after correcting credentials; do not retry unchanged credentials. |
| `400 Bad Request` | Missing/invalid JSON or a required top-level key. | Correct the payload, then submit a new request. | Recoverable after payload correction. No job was created. |
| `429 Too Many Requests` + `Retry-After` | The configured queue limit is full. | Wait at least the advertised delay, then repeat `POST /document`. | Recoverable by retrying later. No job was created. |
| `409 Conflict` + `Retry-After` from `GET /jobs/<id>/result` | The job is still `queued` or `processing`; this is not a failed analysis. | Continue polling the same result URL after the advertised delay. | Recoverable by waiting; never resubmit solely because of this response. |
| Job `state: "failed"` (`GET /jobs/<id>` or HTTP `200` from `/result`) | The worker raised an exception or was restarted while processing. `error` contains a human-readable diagnostic, not a stable machine error code. | Inspect the error and service logs. After any underlying service/input issue is corrected, submit a **new** job using the retained request body. | Terminal for that job. Potentially recoverable only by a new submission; there is no automatic retry. |
| Job `state: "cancelled"` (HTTP `200` from `/result`) | The client cancelled the job, or the worker observed a cancellation request. | Submit a new job only if analysis is still wanted. | Terminal by design; the cancelled job cannot resume. |
| `404 Not Found` for a job endpoint | The id is unknown or its retention period expired; the API intentionally does not distinguish these cases. | If the original request body is available and analysis is still needed, submit it as a new job. | Not recoverable for that job id. |
| HTTP `5xx` or a network failure | The web tier, SQLite store, proxy, or network did not complete the request. The API has no additional structured error contract for these failures. | A client may retry an idempotent `GET`. Before retrying `POST /document`, determine whether a `202` response/job id was received to avoid duplicate analysis. | Usually transient, but do not assume a submission was not accepted without checking the client-side response. |

`POST /similarity` is intentionally different: it returns HTTP `200` for
application failures and places the outcome in `result`. `"fail"` means
malformed input, an unavailable model, or a similarity computation
exception. Correct malformed input and retry; for model/runtime failures,
retry only after the service has recovered. `"yes"` and `"no"` are
successful grading outcomes, not errors.

## Configuration snapshot

This section is a snapshot of the values the service **runs with today**.
Each subsection lists the current defaults and, next to them, the source
file that owns them so you can verify a value has not drifted.

### Cloze classes and quotas

Right now, production is configured to return **15 candidate words per
document**, split across **six real cloze classes** plus one synthetic
fallback category (`random`). All six real classes run with
`LM_verify: True`. Source of truth: `WebAPI/cloze_config.py`.

| Class key           | n | `pos_tags`             | Extra fields                                                                       | Quota | Verified? |
|---------------------|---|------------------------|------------------------------------------------------------------------------------|-------|-----------|
| `unigrams_NOUN`     | 1 | `NOUN`                 | —                                                                                  | 4     | yes       |
| `unigrams_ADJ`      | 1 | `ADJ`                  | —                                                                                  | 4     | yes       |
| `bigrams_NOUN_ADJ`  | 2 | `NOUN`, `ADJ`          | —                                                                                  | 2     | yes       |
| `bigrams_ADV_ADJ`   | 2 | `ADV`, `ADJ`           | —                                                                                  | 1     | yes       |
| `trigrams`          | 3 | `NOUN`, `ADV`, `ADJ`   | —                                                                                  | 2     | yes       |
| `trigrams_w_ADJ`    | 3 | `ADJ`                  | `match: "contains"`, `allowed_pos_tags: [VERB, PRON, NUM, ADJ, NOUN]`               | 2     | yes       |
| *`random`* (fallback) | any | — | not a real class; see [Random fallback](#5-random-fallback)                        | 0*    | no        |

\* `random`'s "quota" is not fixed. It only takes over slots that the six
real classes could not fill.

Plain-language explanation of each class:

- **`unigrams_NOUN`** — 4 single-noun blanks per document. Any word the POS
  tagger labels a noun becomes a candidate ("photosynthesis", "protein",
  "graph", ...). The LM is asked to fill the sentence with the noun
  blanked out; the noun is kept only if the LM's top-40 predictions
  include it.
- **`unigrams_ADJ`** — 4 single-adjective blanks per document. Same idea as
  `unigrams_NOUN` but for words tagged as adjectives ("photosynthetic",
  "single", "atmospheric", ...).
- **`bigrams_NOUN_ADJ`** — 2 two-word blanks per document, where both
  tokens are either a noun or an adjective. So `ADJ NOUN` ("labeled
  graphs"), `NOUN NOUN` ("carbon dioxide"), `ADJ ADJ` ("simple
  photosynthetic"), and `NOUN ADJ` all qualify. A pair with a verb,
  adverb, preposition or article in it is skipped.
- **`bigrams_ADV_ADJ`** — 1 two-word blank per document, where both tokens
  are an adverb or an adjective ("directly polar", "commonly labeled",
  ...). Same rule as above, just a different POS pair.
- **`trigrams`** — 2 three-word blanks per document, where **every** one of
  the three tokens is tagged `NOUN`, `ADV`, or `ADJ`. Kept spans are
  entirely content words (e.g. `NOUN NOUN NOUN`, `ADJ NOUN NOUN`,
  `ADV ADJ NOUN`, ...). Anything with a verb, particle, pronoun or
  numeral is dropped.
- **`trigrams_w_ADJ`** — 2 three-word blanks per document that **contain
  at least one adjective**, where the other two tokens are restricted to
  `VERB`, `PRON`, `NUM`, `ADJ`, `NOUN` (via `allowed_pos_tags`). This lets
  more natural-sounding phrases through ("derive additional features")
  while still enforcing an adjective anchor. `PRT` (particles) is
  **deliberately excluded** so English particles like *to*, *up*, *off*
  cannot slip in alongside an adjective (that used to leak past the
  filter and reach end users).

Scaling the total: the 15-word production total is set by
`_DEFAULT_CLASSES_NUM` in `WebAPI/cloze_config.py` (`4 : 4 : 2 : 2 : 2 : 1`
for the six classes above). Two knobs override it:

- `AVER_CLOZE_TOTAL=N` — scales all six quotas so their sum equals `N`,
  preserving the `4 : 4 : 2 : 2 : 2 : 1` mix. Rounding is applied and then
  a small delta is redistributed to keep the sum exact. Evaluations run
  with `AVER_CLOZE_TOTAL=100`; do **not** set this in production.
- `AVER_CLASSES_NUM='{"unigrams_NOUN":10, ...}'` — overrides the mapping
  wholesale. Its keys must match `PICK_CLASSES` exactly.

### LM inference parameters

MT5-large is called through `MLmethod/MT5_model_method.py`. The relevant
defaults today (all env-tunable, none set in production):

| Parameter              | Env var            | Default | Meaning |
|------------------------|--------------------|---------|---------|
| Beam width             | (hard-coded)       | `AVER_MAX_RANGE` (41) | `num_beams` for the beam-search call. Increases the top-K breadth. |
| Number of returned sequences | (hard-coded) | `AVER_MAX_RANGE` (41) | `num_return_sequences`; one prediction per beam. |
| Max new tokens         | (hard-coded)       | 20      | Beam-search generation cap. |
| Accept window (LM rank) | `AVER_MIN_RANGE` / `AVER_MAX_RANGE` | `1` / `41` | Candidate accepted iff the model's rank of the original word (`ml_pos`) is in `[MIN_RANGE, MAX_RANGE)`. The model returns 41 predictions per blank; the default range keeps ranks 1..40 and **deliberately excludes rank 0** — a word the model already puts at the very top is too predictable to make a useful cloze question, so a `MIN_RANGE=0` would let those slip through. |
| Batch size             | `AVER_BATCH_SIZE`  | 5       | Candidates per model forward pass. Bigger batches = fewer passes at the cost of more RAM per pass. Tune with `scripts/benchmark_batch_size.py`. |

The beam width and max new tokens live inline in
`MLmethod/MT5_model_method.py`; the accept window and batch size are read
from env in `WebAPI/worker.py`. See
[Batch size benchmark](#batch-size-benchmark) for how to pick a value on a
new host.

### Filters

The candidate-quality filter is described in
[Stage 1](#1-sentence-extraction-and-filtering). It has no runtime tunables
today; the patterns are static in `MLmethod/MT5_model_method.py`. The
things you can control:

- **Stopword lists.** One shipped list per language in
  `MLmethod/stopwords_{en,cs,sk}.txt`. English recently gained `a`, `at`,
  `by`, `of`, `to`, and other function words that had been leaking into
  multi-word candidates. Any n-gram that contains a stopword is dropped
  before it can reach the LM.
- **Sentence-length cap.** `AVER_FILTER_MAX_SENTENCE_TOKENS` (default
  unset = no cap; effective ceiling in the analyzer is what MT5 tolerates
  as a prompt). Long, list-like or malformed sentences that pass Stage 1
  can still overwhelm the batches; set this to a token count when a
  specific input causes trouble.
- **Reference-section heuristics.** Configured through the shipped list of
  section headings (`References`, `Bibliography`, `Works Cited`, `Zdroje`,
  `Literatura`, `Referencie`, ...). Static.

### Language auto-detection

Off by default. Enable per host with `AVER_LANGUAGE_AUTO_CORRECT=1` in
`WebAPI/.env`. Detector abstains on `< 8` alphabetic tokens or `< 40`
alphabetic characters. See
[Language auto-detection](#2-language-auto-detection).

### Health thresholds

All health probes are computed in `WebAPI/bottleAPI.py` from `jobs.db`
aggregates; no journald or log parsing. The tunable thresholds — all
env-overridable in `WebAPI/.env`:

| Env var                                    | Default          | Meaning |
|--------------------------------------------|------------------|---------|
| `AVER_HEALTH_HEARTBEAT_STALE_SECONDS`      | `60`             | Worker treated as dead once its last heartbeat is older than this. |
| `AVER_HEALTH_FAILURES_1H_ALERT`            | `10`             | Failed jobs in the last hour that trip `degraded`. |
| `AVER_HEALTH_STUCK_PROCESSING_SECONDS`     | `18000` (5 h)    | A single job in `processing` longer than this trips a problem. Sized against the CPU deployment where a normal run takes 2–3 h. |
| `AVER_HEALTH_QUEUE_BACKLOG_ALERT_SECONDS`  | `172800` (48 h)  | Oldest queued job (measured against `created_at`) waiting longer than this trips a problem. |

Full payload and monitor setup: see
[Health endpoint and uptime monitoring](#health-endpoint-and-uptime-monitoring).

### Environment variable reference

The one place for every variable is `WebAPI/.env` (copy from
`.env.example`). Both processes source it before exec. The tracked
`systemd` unit files carry no `Environment=` lines; putting them there
only invites drift and is unsafe for the API key. Restart both services
after editing (`systemctl restart aver-backend aver-worker`; no
`daemon-reload` needed).

Shared (set on both processes where relevant):

- `AVER_DB_PATH` (default `./jobs.db`): SQLite job-store file. **Both
  processes must point at the same file.**
- `AVER_JOB_TTL_SECONDS` (default `604800` = 7 days): result retention.
- `AVER_LOG_LEVEL` (default `INFO`): `DEBUG`, `INFO`, or `WARNING`.

Web tier (`aver-backend` / `start_api.sh`):

- `AVER_API_KEY`: shared secret required in the `X-API-Key` header. Unset
  = authentication disabled.
- `AVER_PORT` (default `11122`): HTTP port.
- `AVER_WEB_CONCURRENCY` (default `2`): gunicorn worker processes.
- `AVER_GUNICORN_TIMEOUT` (default `300`): gunicorn request timeout in
  seconds. Covers the **whole** request, including reading the upload off
  the socket. See
  [Large uploads and the gunicorn timeout](#large-uploads-and-the-gunicorn-timeout).
- `AVER_GUNICORN_THREADS` (default `4`): threads per gunicorn worker.
  Web-tier request concurrency; **not** the same knob as `AVER_WEB_THREADS`.
- `AVER_WEB_THREADS` (default `1`): CPU threads (`OMP_NUM_THREADS`) for
  the web process. Keep it small so it does not steal cores from the
  worker.
- `AVER_MAX_QUEUE` (default `0`): reject `POST /document` with `429` once
  this many jobs are `queued`/`processing`. `0` = unbounded.
- `AVER_SIMILARITY_MODEL` (default
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`):
  sentence-transformers model id or local path for `/similarity`.
- `AVER_SIMILARITY_THRESHOLD` (default `0.7172`): cosine cutoff for the
  boolean `match` returned by `/similarity`.
- `AVER_HEALTH_*`: see [Health thresholds](#health-thresholds).

Worker (`aver-worker` / `start_worker.sh`):

- `AVER_INFERENCE_BACKEND` (default `torch`): `openvino` or `torch`.
- `AVER_OPENVINO_MODEL_DIR`: path to an exported OpenVINO model.
- `AVER_OPENVINO_EXPORT` (default `0`): `1` to export on first load.
- `AVER_WORKER_THREADS` (default `3`): CPU threads for inference. Give it
  all cores; sets OMP/MKL/OpenBLAS thread counts.
- `AVER_WORKER_POLL_SECONDS` (default `2`): idle poll interval for new
  jobs.
- `AVER_WORKER_CLEANUP_INTERVAL` (default `3600`): how often (seconds) to
  purge expired jobs while idle.
- `AVER_BATCH_SIZE` (default `5`): cloze positions per model forward
  pass. Bigger = fewer passes, more memory per pass. See
  [LM inference parameters](#lm-inference-parameters).
- `AVER_MIN_RANGE` (default `1`), `AVER_MAX_RANGE` (default `41`): top-K
  acceptance window for the LM check. See
  [LM inference parameters](#lm-inference-parameters).
- `AVER_CLOZE_TOTAL`: scale the six per-class quotas so they sum to `N`
  (production stays at 15).
- `AVER_CLASSES_NUM`: JSON override of the whole `CLASSES_NUM` mapping.
- `AVER_LM_DECISION_LOG` (default `./lm_decisions.jsonl`, on): JSONL file
  that receives one line per LM decision. Empty string disables. See
  [LM decision log](#lm-decision-log).
- `AVER_LANGUAGE_AUTO_CORRECT` (default `0`): run the `{en, cs, sk}`
  detector and swap `language` on disagreement. See
  [Language auto-detection](#2-language-auto-detection).
- `AVER_AUTO_RESTART_ON_NEW_COMMIT` (default `0`): worker exits gracefully
  between jobs when a new commit is on disk. See
  [Painless updates](#painless-updates).

GPU (worker, `torch` backend only — see [GPU deployment](#gpu-deployment)):

- `AVER_USE_GPU`: unset/empty = auto (use a GPU when CUDA is available),
  `1` = use a GPU, `0` = force CPU. A GPU is acquired per job and
  released while idle.
- `AVER_GPU_MIN_FREE_GB` (default `14`): minimum free VRAM a GPU must
  have to be picked.
- `AVER_GPU_WHOLE_NODE` (default `1`): `1` prefer a fully-idle GPU; `0`
  allows any GPU that meets the free-VRAM bar.
- `AVER_GPU_WAIT_SECONDS` (default `600` = 10 min): when all GPUs are
  busy, wait this long before retrying.
- `CUDA_VISIBLE_DEVICES`: honored — only GPUs visible to the process are
  picked.

Setup-time only (`setup_api.sh`, used when `AVER_USE_GPU=1`):

- `AVER_TORCH_CUDA` (default `cu121`).
- `AVER_TORCH_VERSION` (default `2.4.1`).

## Deployment

### VM requirements

Baseline for the CPU deployment on Stratus FI (Debian 12 [CVTFI] template):

- Memory: 16 GB
- CPU: 2 cores / 3 vCPU
- System disk: 30 GB (MT5 export ~5 GB, similarity model ~0.5 GB)

The worker holds MT5 for its whole lifetime and peaks to 12–16 GB while
verifying a large document; see [RAM, OOM kills, and swap](#ram-oom-kills-and-swap).

### End-to-end install on a fresh VM

Full sequence to bring up both processes on a clean Debian 12 VM.
`$AVER_DIR_PATH` is the install path (on Stratus: `/app/AVer`).

```sh
# 0. System prerequisites + uv (dependency manager)
apt update && apt install -y git sqlite3 curl
curl -LsSf https://astral.sh/uv/install.sh | sh        # installs uv; restart shell or source its env

# 1. Get the source
git clone <repo-url> $AVER_DIR_PATH        # or copy the tree to $AVER_DIR_PATH
cd $AVER_DIR_PATH/WebAPI

# 2. Create the env (WebAPI/venv) + install the locked deps via uv
#    (CPU-only torch, OpenVINO, sentence-transformers, ...). uv can also fetch a
#    suitable Python itself. Also downloads NLTK data and creates the symlinks.
./setup_api.sh

# 3. Export the MT5 model to OpenVINO INT8 (one-time, slow -- see below)
./venv/bin/optimum-cli export openvino --model google/mt5-large \
  --task text2text-generation-with-past \
  --weight-format int8 $AVER_DIR_PATH/WebAPI/ov_mt5/

# 4. Pre-download the similarity model (needed if the VM is offline at runtime)
./venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
  SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

# 5. Create a service user and hand over ownership
useradd --system --home $AVER_DIR_PATH --shell /usr/sbin/nologin aver
chown -R aver:aver $AVER_DIR_PATH

# 6. Set the runtime config (single source of truth) and install both units.
#    The tracked unit files carry no Environment= lines; everything (API key,
#    backend, model dir, ports, ...) is read from .env.
cp .env.example .env             # then edit .env before starting the services
cp aver-backend.service aver-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aver-backend.service aver-worker.service

# 7. (optional) retention cron
crontab -e
# 0 * * * * $AVER_DIR_PATH/WebAPI/cron_cleanup_jobs.sh >> $AVER_DIR_PATH/WebAPI/cleanup.log 2>&1

# 8. Smoke test
curl -H "X-API-Key: $KEY" -d @sample_requests/test_request.json \
  -H "Content-Type: application/json" http://localhost:11122/document
```

Steps 3 and 4 download large files from the Hugging Face hub; do them once
while the VM has network access.

### Quick setup on an existing box

Dependencies are managed with [uv](https://docs.astral.sh/uv/) via
`WebAPI/pyproject.toml` and the committed `WebAPI/uv.lock` (exact,
reproducible versions). Install uv first if needed:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then:

```sh
cd WebAPI
./setup_api.sh
```

This creates the environment at `WebAPI/venv` and installs the **exact
locked** dependency set with `uv sync --frozen` (including a CPU-only
PyTorch from the pinned PyTorch index, OpenVINO via `optimum-intel`, and
`sentence-transformers` for the similarity endpoint), then downloads the
NLTK data and sets up the `MLmethod` symlinks. It does **not** download
the MT5 or similarity model weights — see steps 3–4 above for those.

To change dependencies, edit `pyproject.toml` and run `uv lock` to refresh
`uv.lock`, then `uv sync`. The env is named `venv` (not uv's default
`.venv`) so the start scripts and systemd units keep their `./venv/bin/...`
paths.

### OpenVINO INT8 export

The API can run MT5 on CPU via OpenVINO. Enable it with env vars:

```sh
export AVER_INFERENCE_BACKEND=openvino
export AVER_OPENVINO_EXPORT=1
```

Notes:

- Set `AVER_OPENVINO_MODEL_DIR` to a pre-exported OpenVINO model directory
  to skip export.
- Exporting `mt5-large` can take time and disk space; do it once and reuse
  the directory.

Recommended export (one-time) using Optimum Intel with INT8 weights:

```sh
optimum-cli export openvino --model google/mt5-large \
  --task text2text-generation-with-past \
  --weight-format int8 /path/to/ov_mt5/
```

Then run with:

```sh
export AVER_INFERENCE_BACKEND=openvino
export AVER_OPENVINO_MODEL_DIR=/path/to/ov_mt5
```

When using a local OpenVINO model directory, leave `AVER_OPENVINO_EXPORT`
unset or set it to `0`.

### Running the two processes manually

In production both run under systemd (see the next two sections). To run
them by hand (for debugging), start **each in its own shell** — they talk
only through the shared SQLite file:

```sh
# Web tier (enqueues jobs, serves status/results + /similarity)
cd WebAPI
export AVER_API_KEY=your-secret AVER_PORT=11122
./start_api.sh

# Inference worker (loads MT5, processes jobs one at a time)
cd WebAPI
export AVER_INFERENCE_BACKEND=openvino AVER_OPENVINO_MODEL_DIR=./ov_mt5
export AVER_DB_PATH=./jobs.db        # must match the web tier
./start_worker.sh
```

The web tier alone will accept jobs but they stay `queued` until the
worker is running. Both must use the same `AVER_DB_PATH`.

Quick smoke test after both are up (set `KEY` to your `AVER_API_KEY`, or
omit the header if auth is disabled):

```sh
curl -H "X-API-Key: $KEY" -d @WebAPI/sample_requests/test_request.json \
  -H "Content-Type: application/json" \
  http://localhost:11122/document
```

Replace `localhost` with your host, e.g. `http://apollo:11122/document`.

### systemd units (root)

Two units are needed: the web tier (`aver-backend`) and the inference
worker (`aver-worker`). The tracked templates in
`WebAPI/aver-backend.service` and `WebAPI/aver-worker.service` are
intentionally minimal — unit metadata, `User`/`Group`, `WorkingDirectory`,
`ExecStart`, and `TimeoutStopSec=infinity` on the worker (see
[Painless updates](#painless-updates)). **All application config lives in
`WebAPI/.env`**; `start_api.sh` and `start_worker.sh` source that file
before they exec the process, so putting `Environment=` lines in the unit
files only invites drift and is unsafe for the API key.

Create a dedicated user and set ownership:

```sh
useradd --system --home $AVER_DIR_PATH --shell /usr/sbin/nologin aver
chown -R aver:aver $AVER_DIR_PATH
```

Replace `$AVER_DIR_PATH` with the install path (on Stratus it is
`/app/AVer`). Prepare `.env` (copy from `.env.example`, set
`AVER_API_KEY`, `AVER_INFERENCE_BACKEND`, `AVER_OPENVINO_MODEL_DIR`,
`AVER_WORKER_THREADS`, and anything else the host needs). Both processes
read the same `.env`, so `AVER_DB_PATH` is defined in exactly one place.
Then install the units:

```sh
cd WebAPI
cp .env.example .env         # then edit .env: API key, backend, model dir, ...
sudo cp aver-backend.service aver-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aver-backend.service aver-worker.service
sudo systemctl status aver-backend aver-worker
```

To change configuration on a running host, edit `.env` and restart — no
`daemon-reload` needed unless the unit file itself changes:

```sh
sudo systemctl restart aver-backend aver-worker
```

Optionally add the retention cron (the worker also purges while idle):

```sh
crontab -e
# 0 * * * * /app/AVer/WebAPI/cron_cleanup_jobs.sh >> /app/AVer/WebAPI/cleanup.log 2>&1
```

### User-level systemd (no root)

On a machine where you cannot install system-wide units (for example a
shared GPU server with no root), run both processes as **user services**
under `systemd --user`. The units live in `~/.config/systemd/user/` and
run as your account — no root, no `/etc/systemd/system/`, no dedicated
`aver` user.

`WebAPI/install_user_services.sh` generates both units pointing at the
current `WebAPI` directory and its `./venv`, then enables and starts
them. The generated units are minimal (no `Environment=` lines); all
configuration lives in `WebAPI/.env`, which the wrappers source at every
start.

```sh
cd /path/to/AVer/WebAPI
cp .env.example .env             # then edit .env: API key, GPU, ports, ...
./setup_api.sh                   # create ./venv first (see GPU note)
./install_user_services.sh
```

Change configuration by editing `.env` and restarting — no need to re-run
the installer:

```sh
systemctl --user restart aver-backend aver-worker
```

Manage the services with the `--user` flag:

```sh
systemctl --user status aver-backend aver-worker
systemctl --user restart aver-worker
journalctl --user -u aver-worker -f
```

**Lingering (important).** By default user services stop when you log out
and do **not** start at boot. To keep them running across logout/reboot,
enable lingering for your account once:

```sh
loginctl enable-linger "$USER"
loginctl show-user "$USER" | grep Linger      # -> Linger=yes
```

`install_user_services.sh` attempts this automatically. If polkit denies
it for a non-admin, ask an administrator to run
`loginctl enable-linger <your-user>` once — everything else stays
user-level. Without lingering the services only run while you have an
active login session.

### GPU deployment

The worker can run MT5 inference on a CUDA GPU. It keeps the model on the
CPU while idle and, for each job, **dynamically picks a GPU** with at
least `AVER_GPU_MIN_FREE_GB` (default 14 GB) free — preferring a
fully-idle card so it does not crowd onto a GPU someone else is using. If
every GPU is busy it waits `AVER_GPU_WAIT_SECONDS` (default 10 min) and
retries. When the job finishes the GPU is released, so the worker holds
no VRAM between jobs. See the "GPU" entries under
[Environment variable reference](#environment-variable-reference) for all
knobs.

Enable it per machine with `AVER_USE_GPU` (`1` = GPU, `0` = CPU, unset =
auto).

**CUDA torch.** The locked dependencies install the **CPU-only** torch
(correct for the CPU VM). On a GPU machine, run setup with `AVER_USE_GPU=1`
so it overwrites torch with a matching CUDA build:

```sh
cd WebAPI
AVER_USE_GPU=1 ./setup_api.sh          # installs torch==2.4.1 from the cu121 wheel index
```

Override the CUDA channel/version if needed with `AVER_TORCH_CUDA` (default
`cu121`) and `AVER_TORCH_VERSION` (default `2.4.1`). If you start the
worker with `AVER_USE_GPU=1` but only CPU torch is installed, it logs a
warning and falls back to CPU.

## Operations

### Painless updates

Two features cooperate to make `git pull` the whole deploy workflow,
without ever interrupting an in-flight job:

1. **`TimeoutStopSec=infinity`** in `aver-worker.service` (already applied
   by the tracked unit files). The worker's `SIGTERM` handler flips
   `_running = False`, which lets the current job finish and then the
   main loop exits. Because systemd waits indefinitely for that drain,
   `systemctl restart aver-worker.service` becomes a safe "drain, then
   restart on the new code" operation. Idle workers exit immediately.
2. **`AVER_AUTO_RESTART_ON_NEW_COMMIT=1`** (off by default; set in
   `.env`). When enabled, the worker records its boot-time git SHA and
   re-queries the working tree on every idle poll. If the SHA has moved,
   the worker exits cleanly *between* jobs and systemd
   (`Restart=always`) starts it back up on the new code. The check runs
   only when the worker is idle, so an in-flight job is never
   interrupted.

Recommended deploy loop for the CPU deployment:

```sh
cd /app/AVer
git pull
# Nothing else. If AVER_AUTO_RESTART_ON_NEW_COMMIT=1 in WebAPI/.env, the
# worker will pick up the new commit as soon as it finishes its current
# job (which may take hours -- that's fine). Check with:
tail -f /var/log/syslog | grep aver-worker   # or `journalctl -u aver-worker -f`
```

If you have the flag off and want the same behaviour manually:

```sh
git pull
systemctl restart aver-backend.service            # cheap, does not touch the worker
systemctl restart aver-worker.service             # drains first (unlimited timeout)
# The second command blocks until the current job finishes. That's expected.
# `systemctl status aver-worker` shows "deactivating" during the drain.
```

The backend (gunicorn) is stateless — restart it any time; in-flight
worker jobs are not affected because they live in a different process.

To watch the drain live: `aver_db.py queue` (shows pending count) and
`curl -s http://localhost:11122/health | jq .worker` (shows `state`,
`current_job_id`, and `version` — the boot-time SHA).

### Health endpoint and uptime monitoring

`GET /health` returns a JSON summary of "is the deployment healthy right
now" and is meant for uptime monitors. The route is **public** — no
`X-API-Key` header required — so external monitors can poll it without a
shared secret. It never reveals per-request contents.

- HTTP `200 OK` with `"status":"ok"` when every probe passes.
- HTTP `503 Service Unavailable` with `"status":"degraded"` when at least
  one probe raised a concern. The full body still ships so an operator
  can see *why* it degraded.

Payload shape (example, worker idle and healthy):

```json
{
  "status": "ok",
  "checked_at": "2026-09-22T13:19:41.234Z",
  "uptime": { "backend_seconds": 87213.4 },
  "db":     { "reachable": true, "path": "./jobs.db" },
  "worker": {
    "alive": true,
    "state": "idle",
    "current_job_id": null,
    "seconds_since_heartbeat": 1.3,
    "process_uptime_seconds": 86790.2,
    "version": "3fa8619"
  },
  "queue": {
    "queued": 0,
    "processing": 0,
    "oldest_queued_seconds": null,
    "oldest_processing_seconds": null
  },
  "recent_failures": { "last_1h": 0, "last_24h": 3 },
  "problems": []
}
```

`problems` is a list of human-readable strings describing what tripped,
e.g. `"worker heartbeat is 82s old (threshold 60s)"`,
`"12 failed job(s) in the last hour (threshold 10)"`,
`"database unreachable"`, or `"queue backlog: oldest queued job is 2100s
old (threshold 1800s)"`. The tunable thresholds are documented in
[Health thresholds](#health-thresholds).

Point an external monitor (UptimeRobot, StatusCake, BetterUptime, ...) at:

```
https://nlp.fi.muni.cz/projekty/aver/api.cgi/health
```

Recommended UptimeRobot configuration (free tier is enough):

1. **Monitor type:** `HTTP(s)` (or `Keyword` if you want the content
   check; both are fine).
2. **URL:** the full public URL above.
3. **Monitoring interval:** 5 minutes.
4. **Keyword monitoring (optional, recommended):** `Keyword type =
   exists`, `Keyword = "status":"ok"`. This catches the (rare) case
   where the endpoint would still return `200` but the upstream
   infrastructure is quietly broken.
5. **Alert contacts:** email and/or a Slack / Teams / Discord webhook.

Behind the scenes:

- The worker writes a singleton row `worker_state` in `jobs.db` on every
  poll (~every 2 s). The health endpoint reads that row to compute
  `worker.alive` and `worker.state`.
- Failure counts, queue depth, and job ages all come from `jobs.db`
  aggregates. No log parsing, no journald access needed. That keeps the
  endpoint fast (a few SELECTs) and portable across hosts.

Local smoke test after a restart:

```sh
curl -s http://127.0.0.1:11122/health | jq .
# Expect status:"ok" and worker.alive:true after the worker finishes loading.
```

### Inspecting the job DB (`aver_db.py`)

`WebAPI/aver_db.py` is a stdlib-only CLI that reads `jobs.db` directly. It
has no dependency on Bottle, gunicorn, or the AVer venv, so plain
`python3` works. Point `--db` somewhere else if the DB is not at the
default `WebAPI/jobs.db` path. Job ids can be given as a unique prefix.

Common subcommands (`python3 aver_db.py <cmd> --help` for full options):

- `queue` — show queued and processing jobs. A `heartbeat` older than a
  minute on a `processing` job means the worker died or got OOM-killed.
- `stats --since N` — throughput and p0 / p50 / p90 / p95 / p99 processing
  latency in the last N hours, plus queue wait and request-size
  percentiles.
- `ls --since N [--state STATE]` — recent jobs with state, size, and
  duration.
- `failed --since N` — jobs with `state=failed` or a set `error` column,
  including the stored traceback.
- `search TERM` — find jobs whose request contains a substring.
- `show ID` — one-screen summary of a job (times, paragraph previews,
  candidates per class). When the response carries a `language_correction`
  block (auto-detection is enabled, see
  [Language auto-detection](#2-language-auto-detection)), it prints
  `declared` / `detected` / `applied` and the per-language scores.
- `paragraphs ID` — list the paragraphs of the request.
- `candidates ID` — every candidate the worker returned, with category
  and (when present) POS tags.
- `leaks ID` — flag candidates whose surface contains a function word
  (auto-detects the English stopword file next to the checkout; pass
  `--stopwords /path/to/list.txt` to override).
- `retag ID` — re-run the POS tagger on the *stored request* and print
  `(word, tag, STOPWORD?)` for each candidate. Works retroactively on
  jobs that predate the `pos_tags` field on the response. Requires
  `nltk`, so run it via the venv:
  `./venv/bin/python aver_db.py retag <id>`.
- `pool ID [--only CLASS] [--show-context]` — replay the extraction step
  locally against the stored request and print every n-gram the analyzer
  would have handed to the LM, grouped by cloze class. Bypasses MT5 (no
  LM verification), uses the real `PICK_CLASSES` from `cloze_config.py`,
  the same POS tagger, and the same stopword list, so the output is the
  exact pre-LM pool. Useful to explain a result that returned no
  bigrams / trigrams: if the pool is non-empty, LM verification rejected
  everything, not the extractor. Requires `nltk` (and `corpy.morphodita`
  for `cs` / `sk`), so run it via the venv:
  `./venv/bin/python aver_db.py pool <id>`. Honors
  `AVER_LANGUAGE_AUTO_CORRECT`.
- `request ID`, `result ID` — dump raw JSON to stdout.

Typical uses:

```sh
cd WebAPI
python3 aver_db.py queue                     # what is pending right now?
python3 aver_db.py stats --since 168         # last 7 days of throughput / latency
python3 aver_db.py failed --since 168        # what died in the last week

# investigate a leak reported for a specific document
python3 aver_db.py search "Sugar2sugar"
python3 aver_db.py show   <id>               # incl. language_correction if any
python3 aver_db.py leaks  <id>
./venv/bin/python aver_db.py retag <id>      # tags each candidate's tokens
./venv/bin/python aver_db.py pool  <id>      # raw n-gram pool before the LM
```

Limitation: rejected sentences (from the candidate-quality filter) are
not stored in the DB, only in the worker journal. To correlate
rejections with a document, grep
`journalctl -u aver-worker | grep -E 'Rejected|Text filter'` around the
job's `created_at`.

### LM decision log

The worker appends one JSON line to `AVER_LM_DECISION_LOG` (default
`WebAPI/lm_decisions.jsonl`, on) for each candidate the language model
was actually invoked on. This is the raw dataset for building a cheap
pre-filter or re-ranker: on production traffic today, roughly 8–9
candidates in 10 come back `reject`, so most of the LM cycles are spent
proving the same "no". A classifier trained on this data can either drop
the worst candidates before the LM sees them, or re-order the queue so
the LM meets its quota with fewer batches (safer, no candidate is ever
lost).

Each line carries: timestamp, `job_id`, declared and applied language,
`cloze_class`, the candidate `word`, its POS tag string, its
paragraph / sentence / token index, the source sentence, the blanked
sentence the model saw, the `decision`
(`accept` / `spare` / `reject` / `reject_dup`), `ml_pos`, and the full
top-K `predictions` (word + probability). Candidates that never reach
the LM (early break, phase C random fallback, duplicate suppression
before inference) are **not** logged — by design, so the dataset is
exactly "cases where the LM had an opinion".

Quick audits with `jq`:

```sh
# Overall acceptance rate.
jq -r '.decision' lm_decisions.jsonl | sort | uniq -c

# Per-class acceptance rate.
jq -r '[.cloze_class, .decision] | @tsv' lm_decisions.jsonl \
  | sort | uniq -c | sort -k2

# Which words does the LM prefer over ours the most often? (Regression
# canary for stopword / filter bugs.)
jq -c 'select(.decision=="reject") | .predictions[:3][] | .word' \
  lm_decisions.jsonl | sort | uniq -c | sort -nr | head -20
```

The file is line-buffered, so a hard kill loses at most one line. Growth
is about 50–100 KB per job; on a production host, rotate it with
`logrotate`:

```
# /etc/logrotate.d/aver-lm-decisions
/app/AVer/WebAPI/lm_decisions.jsonl {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
```

Disable per-host by setting `AVER_LM_DECISION_LOG=` (empty) in
`WebAPI/.env`. Failures to write (disk full, permission denied) log a
WARNING and disable the sink for the rest of the worker's lifetime; they
never fail a job.

### RAM, OOM kills, and swap

The worker is the memory-hungry process: it holds the MT5/OpenVINO
weights for its whole lifetime and, on top of that, the full candidate
pool and the LM verification batches for the job it is running. The web
tier does not load the model and stays small.

Observed on the Stratus host (15.6 GB RAM): steady-state ~1 GB idle
after model load, but **12–16 GB while verifying a large document**.
Growth tracks the number of candidates rather than the raw document size
— a 107 KB / 56-paragraph request reached 13 GB at 4/260 progress. When
RSS approaches total RAM the kernel OOM-killer takes the worker out.

**Confirming an OOM kill.** systemd reports the cause, and the kernel
logs the victim:

```sh
journalctl -u aver-worker --since '1 day ago' | grep "Failed with result 'oom-kill'"
journalctl -k --since '1 day ago' | grep -E "Out of memory: Killed|oom-kill:"
```

A kernel line like

```
Out of memory: Killed process 352226 (pt_main_thread) total-vm:22982348kB, anon-rss:15904644kB
```

means the worker was holding ~15.9 GB of anonymous memory when it died.
Note the process name is `pt_main_thread`, not `python`. The affected
job is left as `failed` with "worker restarted during processing".

#### Adding swap

Swap does not make the worker use less memory — it buys headroom so a
peak spills to disk instead of killing the process. Inference that runs
from swap is much slower than from RAM, so treat it as a crash guard,
not as capacity.

The host originally had a single 2 GB swap partition. An 8 GB swapfile
was added on top of it (total 10 GB), on ext4:

```sh
# 1. Allocate, lock down the permissions, format, enable.
#    fallocate is fine on ext4; use `dd if=/dev/zero of=/swapfile bs=1M count=8192`
#    if swapon rejects the file (unwritten extents on some kernels).
fallocate -l 8G /swapfile
chmod 600 /swapfile          # mkswap refuses a world-readable file
mkswap /swapfile
swapon --priority 10 /swapfile   # higher prio than the 2 GB partition -> used first

# 2. Persist across reboots.
cp /etc/fstab /etc/fstab.bak
echo '/swapfile none swap sw,pri=10 0 0' >> /etc/fstab
findmnt --verify --fstab     # must report 0 errors before you reboot

# 3. Prefer evicting page cache over the hot inference tensors.
echo 'vm.swappiness=10' > /etc/sysctl.d/99-aver-swap.conf
sysctl -w vm.swappiness=10

# 4. Verify.
swapon --show
free -h
```

`vm.swappiness=10` is deliberate: the OpenVINO weights are file-backed
and cheap to re-read, so the kernel should drop those pages before
swapping out the anonymous working set.

To remove the swapfile again: `swapoff /swapfile && rm /swapfile`, then
delete the `/etc/fstab` line.

#### Host-level failure and memory logs

Two host-level logs record what the worker was doing when it died.
Neither is part of the Python application; both are installed as root on
the deployment host.

| file | contents |
| --- | --- |
| `/var/log/aver-failures.log` | one entry per abnormal worker exit: systemd result (`oom-kill` / `timeout` / ...), kernel OOM lines, a memory snapshot, and the in-flight job (id, language, document, request bytes, paragraphs, progress) |
| `/var/log/aver-memory.log` | one line per minute: worker RSS, per-process swap, cgroup current/peak, `MemAvailable`, and the job being processed |

Wiring:

- `/usr/local/bin/aver-failure-report.sh`, invoked from a drop-in at
  `/etc/systemd/system/aver-worker.service.d/10-failure-report.conf` as
  `ExecStopPost=+/usr/local/bin/aver-failure-report.sh`. It must be
  `ExecStopPost=`, **not** `OnFailure=`: the unit uses `Restart=always`,
  so it never enters the `failed` state and `OnFailure=` would never
  run. The `+` prefix runs the hook as root even though the service
  runs as `User=aver`.
- `/usr/local/bin/aver-mem-sample.sh`, driven by `aver-mem-sample.timer`
  (`OnUnitActiveSec=1min`).
- Both rotate via `/etc/logrotate.d/aver` (weekly, 20 MB cap, 4
  generations).

Useful queries:

```sh
# what killed the worker, and on which input
tail -40 /var/log/aver-failures.log

# memory growth within the current job
grep -v '(idle)' /var/log/aver-memory.log | tail -30
```

#### Reducing the peak

Swap and logging only contain the problem. To actually lower the
ceiling:

- **Lower `AVER_BATCH_SIZE`.** LM verification batches dominate the
  transient peak; see [Batch size benchmark](#batch-size-benchmark)
  below to measure the trade-off on the host.
- **Cap the candidate pool.** A large document can produce thousands of
  candidates per cloze class (a 308-paragraph thesis yielded ~7 000).
  Lowering `AVER_CLASSES_NUM` / `AVER_CLOZE_TOTAL` reduces how many are
  verified.
- **Split very large documents** across several requests. The queue
  processes one job at a time, so smaller jobs also lose less work when
  one fails.
- **Add `MemoryHigh=` to the unit** (for example `MemoryHigh=12G`). The
  cgroup then throttles and reclaims into swap instead of tripping the
  global OOM killer, which is free to pick a different victim process
  on the host.
- **Requeue instead of finish on `SIGTERM`.** Returning the in-flight
  job to `queued` and exiting immediately would make restarts instant
  and lossless, and would remove the `timeout` failure mode entirely.

### Large uploads and the gunicorn timeout

**`[CRITICAL] WORKER TIMEOUT (pid:...)` in `journalctl -u aver-backend`,
and large documents fail to submit.** This is gunicorn killing one of
its own *web* processes — it is unrelated to `aver-worker`, despite the
shared word "worker".

`--timeout` is a liveness watchdog: the arbiter kills any worker whose
heartbeat is older than the timeout. The default `sync` worker class
writes that heartbeat only at the top of its accept loop, so it stays
silent for the entire request — **including the time spent reading the
request body off the socket**. A ~1 MB document (`POST /document` sends
the whole text as JSON) on a slow uplink can burn most of a minute in
transfer alone, before `add_doc()` runs at all. The web tier does no
inference, so the old 60 s default looked generous; it was not, because
upload time is inside the budget.

Two changes address it:

- `AVER_GUNICORN_TIMEOUT` now defaults to **300 s**, giving slow clients
  room.
- The web tier now runs the **`gthread`** worker class (`-k gthread`
  with `--threads $AVER_GUNICORN_THREADS`). Its accept loop keeps
  writing the heartbeat while requests execute in a thread pool, so a
  request blocked on a slow upload no longer looks like a hung process.

`gthread` means several requests share one process, so `get_conn()` in
`bottleAPI.py` guards its lazy SQLite open with a lock. The connection
itself is already opened `check_same_thread=False` with writes
serialized inside `job_store`.

If uploads still time out, the durable fix is a buffering reverse proxy
(nginx with `proxy_request_buffering on`) so gunicorn only ever sees a
fully-received body. Note also that bodies over `MEMFILE_MAX` (10 MB,
set in `bottleAPI.py`) are rejected by bottle before the handler sees
them.

### Batch size benchmark

Use the benchmark script to probe `batch_size` on the current machine.
It measures wall time, CPU time, max RSS, percentiles, and CPU/IO
metrics when available.

```sh
cd WebAPI
./venv/bin/python ../scripts/benchmark_batch_size.py \
  --json sample_requests/test_request.json \
  --sizes 1,2,4 \
  --max-lengths 512 \
  --num-beams 21 \
  --max-new-tokens 20 \
  --threads auto \
  --repeat 2 \
  --warmup 1 \
  --json-out benchmark_results.json
```

Notes:

- `--max-lengths` truncates paragraph tokens to test sequence length
  sensitivity.
- `--threads` sets OMP/MKL/OPENBLAS/NUMEXPR thread counts (use `auto`
  for defaults).
- `--num-beams` and `--max-new-tokens` control generation settings for
  timing splits.
- If `psutil` is installed, the script also reports per-process RSS,
  CPU percent, and IO counters.

### Troubleshooting

If `./setup_api.sh` reports `uv: not found`, install uv and rerun:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
# restart the shell (or `source ~/.local/bin/env`) so `uv` is on PATH
cd WebAPI
./setup_api.sh
```

If `uv sync` fails because the lockfile is out of date with
`pyproject.toml` (for example after editing dependencies), refresh it:
`uv lock` then `uv sync`.

**Jobs stay `queued` forever.** The worker is not running or points at
a different `AVER_DB_PATH`. Check it:

```sh
systemctl status aver-worker
journalctl -u aver-worker -f          # model load + per-job logs
./venv/bin/python job_store.py stats  # counts by state in the DB
```

**A job comes back `failed` with "worker restarted during processing".**
The worker stopped mid-job. There is no automatic retry — resubmit the
document. This one error string covers two very different causes, and
they need different fixes, so always confirm which one you hit:

```sh
journalctl -u aver-worker --since '1 hour ago' | grep 'Failed with result'
```

- `Failed with result 'oom-kill'` — the kernel killed the worker; it
  ran out of RAM. See
  [RAM, OOM kills, and swap](#ram-oom-kills-and-swap).
- `Failed with result 'timeout'` — **systemd** killed the worker during
  a stop or restart. This is not a memory problem. On `SIGTERM` the
  worker logs `Received signal 15, finishing current job then exiting`
  and tries to finish the job it is holding. The tracked unit files
  ship with `TimeoutStopSec=infinity` so this drain has unlimited time,
  and `systemctl restart aver-worker.service` is safe mid-job. If you
  see this error on an older host, its `.service` file still has the
  systemd default of 90 s — re-run `install_user_services.sh` (or copy
  the fresh `WebAPI/aver-worker.service` into `/etc/systemd/system/`)
  and `systemctl daemon-reload`. Emergency stop is still
  `systemctl kill`.

**The worker takes a long time to become ready.** It loads MT5 at
startup; the OpenVINO INT8 export (`AVER_OPENVINO_MODEL_DIR`) loads
fastest. The web tier does **not** load MT5, so `/document`,
`/jobs/...`, and the API itself stay responsive during worker startup.

**First `/similarity` call is slow.** The embedding model loads lazily
in the web process on first use; warm it up after start (see
[POST /similarity](#post-similarity)).

To change log verbosity on either process, set `AVER_LOG_LEVEL` (`INFO`
or `DEBUG`) before starting it.

## Development

### Repo layout

Runtime-relevant paths (everything you touch to change service
behaviour):

| Path                                    | Purpose |
|-----------------------------------------|---------|
| `WebAPI/`                               | The service. Contains both processes, the SQLite store, the config, the systemd unit templates, and helper scripts. |
| `WebAPI/bottleAPI.py`                   | Web tier. HTTP routing, request validation, `/health`. |
| `WebAPI/worker.py`                      | Inference worker main loop. Model load, job claim, progress reporting, graceful shutdown. |
| `WebAPI/job_store.py`                   | SQLite schema and every DB access. WAL mode. |
| `WebAPI/cloze_config.py`                | `PICK_CLASSES` and `CLASSES_NUM` (production defaults live here). |
| `WebAPI/similarity.py`                  | Sentence-similarity backend for `/similarity`. |
| `WebAPI/gpu.py`                         | Dynamic GPU acquisition/release for the `torch` backend. |
| `WebAPI/aver_db.py`                     | stdlib-only CLI for `jobs.db`. |
| `WebAPI/aver-backend.service`, `WebAPI/aver-worker.service` | Systemd unit templates (root install). |
| `WebAPI/install_user_services.sh`       | Generator for the user-level systemd units. |
| `WebAPI/setup_api.sh`, `start_api.sh`, `start_worker.sh` | Install and process-entry scripts. |
| `WebAPI/.env.example`                   | Template for `WebAPI/.env`. Single source of runtime config. |
| `WebAPI/sample_requests/`               | Example request payloads used by curl, tests, and the benchmark. |
| `MLmethod/`                             | The analyzer. `MT5_model_method.py` (filter + POS + n-grams + LM verify + fallback) and `language_detection.py`. Imported by `WebAPI/worker.py`. |
| `tests/unit/`                           | Offline unit tests (stdlib `unittest`, no MT5). |
| `tests/regression/`                     | API-based regression harness against a running backend. |
| `scripts/`                              | Utility scripts, including `benchmark_batch_size.py`. |
| `datasets/`                             | Labeled corpora used by the regression suite. |

Other top-level directories (`analysis/`, `Author-Reviewer-Tester-Analysis/`,
`Data Analysis/`, `Nor Education/`, `TACR Project/`, `Team meetings/`,
`Publications/`, `literature_review/`, `User_study/`,
`Ngram_experiments/`, ...) hold historical notebooks, meeting notes, and
one-off experiments. They are **not** part of the runtime and can be
ignored when deploying or extending the service.

### Running the tests

Two test suites live under `tests/` at the repo root:

- **`tests/unit/`** — offline unit tests, standard-library `unittest`
  (no extra dependencies). Does **not** load the MT5 model — the model
  and analyzer are replaced by fakes/stubs — so the whole suite finishes
  in well under a second.
- **`tests/regression/`** — API-based regression harness that submits a
  labeled dataset to a running AVer backend. See
  [`tests/regression/README.md`](tests/regression/README.md) for the
  runbook.

Run the unit suite from the repo root:

```sh
./WebAPI/venv/bin/python -m unittest discover -s tests/unit -t .
```

What the unit suite covers:

- `test_job_store.py` — the SQLite store: enqueue, FIFO claim, progress,
  finish/fail, orphan reset, expiry/cleanup.
- `test_analysis.py` — POS matching (`all` vs `contains`), the
  deficit-fill order (plausible → cross-category → random), and
  `scan_document` per-class separation, `cloze_class` tagging, and
  frequency ordering.
- `test_api.py` — the web endpoints over a direct WSGI client: API-key
  auth, `202`/`400`/`404`/`409`/`429`, and result payloads.
- `test_e2e.py` — full flow (submit → worker processes → poll → fetch)
  with a fake analyzer, including the failure path and orphan recovery.
- `test_health.py` — `/health` payload, degraded conditions, and
  API-key-bypass on the public route.
- `test_auto_restart.py` — `_current_commit()` helper and the
  auto-restart-on-new-commit predicate.
- `test_similarity.py` — similarity helper; skipped unless
  `sentence-transformers` is installed **and**
  `AVER_RUN_SIMILARITY_TESTS=1` (it downloads a model on first use).
