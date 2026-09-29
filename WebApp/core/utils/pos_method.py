"""Classify a MUNI API word into one of the 6 calibrated selection methods.

The MUNI API returns word content + a `predictions` field marking ML
selections, but no POS metadata. To pick the right pA/pN from Hitzinger
2026-05-25, we POS-tag the content ourselves via stanza (which has CS/SK
support; spaCy core_news models don't cover those languages).

Returns a `selection_method` string matching keys in
`core.authorship.SELECTOR_PROBS`, or `None` if classification fails (scoring
then applies `core.authorship.FALLBACK_PROBS`).

Stanza models live under `$STANZA_RESOURCES_DIR` (defaults to
`~/stanza_resources/`). The Dockerfile pre-bakes them at image build time;
locally and in CI the pipeline auto-downloads the first time it needs a
language, then caches for subsequent calls.
"""

from __future__ import annotations

import logging
import threading
from typing import Literal

logger = logging.getLogger(__name__)

# UD POS tags used by stanza.
_NOUN = "NOUN"
_ADJ = "ADJ"
_ADV = "ADV"

# Method keys mirror the granular SELECTOR_PROBS entries.
MethodKey = Literal[
    "ml_noun_unigram",
    "ml_adj_unigram",
    "bigram",
    "adj_adv_trigram",
    "noun_adj_adv_trigram",
    "noun_adj_trigram",
    "trigram_with_adj",
]

# Languages we have stanza pipelines for. Anything else returns None and
# scoring applies authorship.FALLBACK_PROBS.
SUPPORTED_LANGUAGES = frozenset({"cs", "sk", "en"})

_pipelines: dict[str, object] = {}
_pipeline_lock = threading.Lock()


def ensure_model(language_code: str) -> bool:
    """Download the stanza tokenize+pos model for `language_code` if missing.

    Returns True on success (model is now available), False otherwise.
    Safe to call repeatedly — stanza's downloader no-ops when the model
    is already on disk. Stanza writes under `$STANZA_RESOURCES_DIR` (or
    its own `~/.cache/stanza/.../resources` default if unset).
    """
    if language_code not in SUPPORTED_LANGUAGES:
        return False
    try:
        import stanza
    except ImportError:
        logger.warning("pos_method: stanza not installed; method tagging disabled")
        return False
    try:
        stanza.download(
            lang=language_code,
            processors="tokenize,pos",
            verbose=False,
        )
        return True
    except Exception:
        logger.exception(
            "pos_method: failed to download stanza model for %r", language_code
        )
        return False


def _get_pipeline(language_code: str):
    """Lazy-load (and cache) a stanza tokenize+pos pipeline for `language_code`.

    First call per language may download the model if it's not already
    on disk. Subsequent calls reuse the cached pipeline instance. Returns
    None if stanza or the model is unavailable — callers must tolerate
    that; such words are scored with authorship.FALLBACK_PROBS.
    """
    if language_code not in SUPPORTED_LANGUAGES:
        return None
    if language_code in _pipelines:
        return _pipelines[language_code]
    with _pipeline_lock:
        if language_code in _pipelines:
            return _pipelines[language_code]
        try:
            import stanza
        except ImportError:
            logger.warning("pos_method: stanza not installed; method tagging disabled")
            _pipelines[language_code] = None
            return None
        try:
            pipeline = stanza.Pipeline(
                lang=language_code,
                processors="tokenize,pos",
                tokenize_pretokenized=False,
                verbose=False,
                # REUSE_RESOURCES = use cached models, download if missing.
                download_method=stanza.DownloadMethod.REUSE_RESOURCES,
            )
        except Exception:
            logger.exception(
                "pos_method: failed to load stanza pipeline for %r", language_code
            )
            _pipelines[language_code] = None
            return None
        _pipelines[language_code] = pipeline
        return pipeline


def _pos_tags(content: str, language_code: str) -> list[str] | None:
    """Return the UD POS tag per whitespace-split token in `content`.

    Returns None when no pipeline is available. Returns an empty list for
    empty input. Stanza may split punctuation/clitics into extra tokens —
    we only keep tags for tokens whose text matches the original splits.
    """
    pipeline = _get_pipeline(language_code)
    if pipeline is None:
        return None
    expected = content.split()
    if not expected:
        return []
    try:
        doc = pipeline(content)
    except Exception:
        logger.exception("pos_method: stanza failed on %r (%s)", content, language_code)
        return None
    tags: list[str] = []
    pos_by_text: dict[str, str] = {}
    for sentence in doc.sentences:
        for word in sentence.words:
            pos_by_text.setdefault(word.text, word.upos or "")
    for token in expected:
        tags.append(pos_by_text.get(token, ""))
    return tags


def classify_method(
    *,
    content: str,
    language_code: str,
) -> MethodKey | None:
    """Map a MUNI-selected word to one of the 7 calibrated subgroups.

    Rules, derived from Hitzinger's calibration data:
      • 1 token + NOUN  → ml_noun_unigram         (any unigram is ML)
      • 1 token + ADJ   → ml_adj_unigram
      • 2 tokens        → bigram                  (no further POS split)
      • 3 tokens with {NOUN, ADJ, ADV} ⊆ POS-set  → noun_adj_adv_trigram
      • 3 tokens with {ADJ, ADV} ⊆ POS-set (no NOUN) → adj_adv_trigram
      • 3 tokens with {NOUN, ADJ} ⊆ POS-set (no ADV) → noun_adj_trigram
      • 3 tokens with ADJ                          → trigram_with_adj
      • everything else                            → None  (drop from scoring)

    This is the fallback path: a MUNI word normally takes its bucket from the
    `category` the service reports (see core.utils.muni_category), and this
    runs only when that category is missing or not one we map.
    """
    tokens = content.split()
    n = len(tokens)
    if n == 0 or n > 3:
        return None

    if n == 1:
        tags = _pos_tags(content, language_code)
        if tags is None or len(tags) != 1:
            return None
        tag = tags[0]
        if tag == _NOUN:
            return "ml_noun_unigram"
        if tag == _ADJ:
            return "ml_adj_unigram"
        return None

    if n == 2:
        # All bigrams share one calibrated value; no POS tagging needed.
        return "bigram"

    # n == 3 — needs POS to discriminate the trigram subgroups.
    tags = _pos_tags(content, language_code)
    if tags is None or len(tags) != n:
        return None
    tag_set = set(tags)
    if {_NOUN, _ADJ, _ADV} <= tag_set:
        return "noun_adj_adv_trigram"
    if {_ADJ, _ADV} <= tag_set:
        return "adj_adv_trigram"
    if {_NOUN, _ADJ} <= tag_set:
        return "noun_adj_trigram"
    if _ADJ in tag_set:
        return "trigram_with_adj"
    return None
