"""Lightweight language detection for AVer.

Chooses one of ``"en"``, ``"cs"``, ``"sk"`` from a document. The AVer worker
uses this to auto-correct requests whose declared language does not match
their content, because a wrong language silently activates the wrong POS
tagger and the wrong stopword list, which then lets function words leak
into n-gram candidates.

Design goals:
    - Zero new runtime dependencies. Uses the stopword lists already shipped
      under ``MLmethod/stopwords_tagger/`` and a small set of language-exclusive
      Latin-alphabet-with-diacritic characters.
    - Cheap. One linear pass over the tokenized text and one over the
      characters; the whole detector runs in a few milliseconds per document.
    - Explainable. Returns the per-language score vector so operators can
      audit borderline decisions in the log.

Design tradeoffs (worth knowing before you touch this file):
    - The character features are heavily weighted (``CHAR_WEIGHT``). A single
      ``ř`` in an otherwise ambiguous document is enough to flip the decision
      to Czech. This is intentional: those letters occur nowhere else in the
      three-language set the tool supports.
    - Empty or very short input has no useful signal. The detector returns
      the ``fallback`` argument (default: ``None``) with a margin of ``0.0``.
      Callers should treat that as "keep the declared language".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Sequence, Tuple

# --- Stopword loading ------------------------------------------------------
# We accept an already-loaded stopword dict (analyzer state) or fall back to
# reading the shipped files. Keeping both paths lets the tests exercise the
# detector without side effects.

_STOPWORD_FILES = {
    "en": "english_stopwords.txt",
    "cs": "czech_stopwords.txt",
    "sk": "slovak_stopwords.txt",
}


def load_stopwords(stopwords_dir: Optional[str] = None) -> Dict[str, frozenset[str]]:
    """Return ``{lang: frozenset(words)}`` from the shipped stopword files.

    ``stopwords_dir`` defaults to the ``stopwords_tagger`` directory that sits
    next to this module. The values are lowercased and frozen so callers can
    treat them as immutable lookup sets.
    """
    if stopwords_dir is None:
        stopwords_dir = os.path.join(os.path.dirname(__file__), "stopwords_tagger")
    out: Dict[str, frozenset[str]] = {}
    for lang, fname in _STOPWORD_FILES.items():
        path = os.path.join(stopwords_dir, fname)
        with open(path, encoding="utf8") as fh:
            words = [line.strip().lower() for line in fh if line.strip()]
        out[lang] = frozenset(words)
    return out


def _normalize_stopwords(
    stopwords: Dict[str, Iterable[str]],
) -> Dict[str, frozenset[str]]:
    """Accept the analyzer's ``{lang: [word, ...]}`` and return frozensets.

    The analyzer stores stopwords as ``list[str]``. Detection needs O(1)
    membership lookups, so we freeze them once here.
    """
    return {
        lang: frozenset(w.lower() for w in words if w)
        for lang, words in stopwords.items()
    }


# --- Feature definitions ---------------------------------------------------
# Character sets exclusive to each language across the {en, cs, sk} triad.
# These lists are conservative on purpose: only characters that unambiguously
# belong to one of the two Slavic languages are listed. Letters that both
# Czech and Slovak share (á, í, é, ú, ý, č, š, ž, ...) contribute zero on
# their own; the stopword feature disambiguates those cases.

CZECH_EXCLUSIVE = frozenset("ěřůĚŘŮ")
SLOVAK_EXCLUSIVE = frozenset("ľĺŕôäĽĹŔÔÄ")

# Weight for character-based evidence, in units of "stopword hit rate".
# One ``ř`` in a hundred characters contributes ``0.05`` to score(cs), which
# is comparable to a couple of extra stopword hits per hundred tokens.
CHAR_WEIGHT = 5.0

# Minimum tokens / characters below which we abstain. Short strings can flip
# the decision on a single accented character, which is not the operator's
# intent when auto-correcting a request.
MIN_TOKENS = 8
MIN_CHARS = 40

# Simple word tokenizer for the detector. We deliberately do *not* call
# ``nltk.word_tokenize`` here because that pulls in language-specific rules
# and would create a chicken-and-egg dependency (tokenizing to detect the
# language you would tokenize for). Whitespace + punctuation split is
# sufficient for stopword membership checks.
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


# --- Detection -------------------------------------------------------------


@dataclass(frozen=True)
class DetectionResult:
    """Outcome of a single detection call.

    Attributes:
        language: One of ``"en"``, ``"cs"``, ``"sk"``, or ``None`` when the
            input was too short to score reliably.
        scores: Per-language score. Not a probability. Same units for all
            three entries so the ratio and the difference are comparable.
        margin: ``top_score - runner_up_score``. Useful in the log when
            deciding whether a borderline call is trustworthy.
        n_tokens: Alphabetic tokens seen. Below ``MIN_TOKENS`` the detector
            abstains.
        n_chars: Alphabetic characters seen. Below ``MIN_CHARS`` the detector
            abstains.
    """

    language: Optional[str]
    scores: Dict[str, float]
    margin: float
    n_tokens: int
    n_chars: int

    def to_log_dict(self) -> Dict[str, object]:
        """Compact representation for structured log lines."""
        return {
            "language": self.language,
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "margin": round(self.margin, 4),
            "n_tokens": self.n_tokens,
            "n_chars": self.n_chars,
        }


def detect_language(
    text: str,
    stopwords: Optional[Dict[str, Iterable[str]]] = None,
    *,
    fallback: Optional[str] = None,
    min_tokens: int = MIN_TOKENS,
    min_chars: int = MIN_CHARS,
) -> DetectionResult:
    """Detect the majority language of ``text`` across ``{en, cs, sk}``.

    ``stopwords`` accepts the analyzer's ``{lang: list[str]}`` mapping so we
    can reuse the exact word lists the tagger uses. When omitted, the shipped
    files are read once.

    ``fallback`` is returned as ``language`` when the input is below the
    minimum length. The caller sees exactly what they should do: keep the
    declared language and log low confidence.

    The function is pure; it does not touch disk after the initial stopword
    load and it never raises on empty input.
    """
    if stopwords is None:
        stopword_sets = load_stopwords()
    else:
        stopword_sets = _normalize_stopwords(stopwords)

    tokens = _tokenize(text)
    n_tokens = len(tokens)
    n_chars = sum(ch.isalpha() for ch in text)

    if n_tokens < min_tokens or n_chars < min_chars:
        return DetectionResult(
            language=fallback,
            scores={lang: 0.0 for lang in ("en", "cs", "sk")},
            margin=0.0,
            n_tokens=n_tokens,
            n_chars=n_chars,
        )

    # Feature 1: stopword hit rate per language.
    hits: Dict[str, int] = {"en": 0, "cs": 0, "sk": 0}
    for tok in tokens:
        for lang, sw in stopword_sets.items():
            if tok in sw:
                hits[lang] += 1
    stopword_rate = {lang: hits[lang] / n_tokens for lang in hits}

    # Feature 2: exclusive-character rate per Slavic language.
    cs_chars = sum(1 for ch in text if ch in CZECH_EXCLUSIVE)
    sk_chars = sum(1 for ch in text if ch in SLOVAK_EXCLUSIVE)
    cs_char_rate = cs_chars / n_chars
    sk_char_rate = sk_chars / n_chars

    # Combined score:
    #   - English is *penalized* by any exclusive Slavic character in the
    #     document. This is what stops an English document with a couple of
    #     Czech-shared stopwords ("i", "a") from beating real Czech.
    #   - Czech and Slovak are each *boosted* by their exclusive characters.
    scores = {
        "en": stopword_rate["en"] - CHAR_WEIGHT * (cs_char_rate + sk_char_rate),
        "cs": stopword_rate["cs"] + CHAR_WEIGHT * cs_char_rate,
        "sk": stopword_rate["sk"] + CHAR_WEIGHT * sk_char_rate,
    }

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_lang, top_score = ordered[0]
    runner_up_score = ordered[1][1]

    # Refuse to pick a winner when the top score is not positive. That state
    # means neither stopword nor character evidence was found, which happens
    # for pure code / tables / URLs.
    if top_score <= 0.0:
        return DetectionResult(
            language=fallback,
            scores=scores,
            margin=top_score - runner_up_score,
            n_tokens=n_tokens,
            n_chars=n_chars,
        )

    return DetectionResult(
        language=top_lang,
        scores=scores,
        margin=top_score - runner_up_score,
        n_tokens=n_tokens,
        n_chars=n_chars,
    )


def detect_language_for_paragraphs(
    paragraphs: Sequence[str],
    stopwords: Optional[Dict[str, Iterable[str]]] = None,
    *,
    fallback: Optional[str] = None,
) -> DetectionResult:
    """Convenience wrapper that joins paragraphs and calls ``detect_language``.

    ``scan_document`` gets a list of paragraph strings. Joining once here is
    cheaper than tokenizing each paragraph separately and gives the detector
    the largest possible sample.
    """
    joined = "\n".join(p or "" for p in paragraphs)
    return detect_language(joined, stopwords=stopwords, fallback=fallback)


__all__ = [
    "CHAR_WEIGHT",
    "CZECH_EXCLUSIVE",
    "DetectionResult",
    "MIN_CHARS",
    "MIN_TOKENS",
    "SLOVAK_EXCLUSIVE",
    "detect_language",
    "detect_language_for_paragraphs",
    "load_stopwords",
]
