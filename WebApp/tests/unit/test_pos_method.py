"""Unit tests for the stanza-backed POS method classifier.

Skipped when the relevant stanza models aren't installed (e.g. CI without
the predownloaded models), so dev environments without ~200MB of CS
weights aren't blocked.
"""

from __future__ import annotations

import pytest

from core.utils.pos_method import classify_method


def _stanza_available(language: str) -> bool:
    try:
        import stanza
    except ImportError:
        return False
    try:
        stanza.Pipeline(
            lang=language,
            processors="tokenize,pos",
            verbose=False,
            download_method=None,
        )
        return True
    except Exception:
        return False


pytestmark_cs = pytest.mark.skipif(
    not _stanza_available("cs"),
    reason="stanza CS model not installed",
)


@pytestmark_cs
def test_unigram_noun_cs():
    # 1 token + NOUN → ml_noun_unigram (regardless of predictions flag)
    assert classify_method(content="duby", language_code="cs") == "ml_noun_unigram"


@pytestmark_cs
def test_unigram_adjective_cs():
    # 1 token + ADJ → ml_adj_unigram
    assert classify_method(content="mírná", language_code="cs") == "ml_adj_unigram"


def test_bigram_no_pos_tagging_needed():
    # All bigrams collapse to one calibrated value; the classifier should
    # return "bigram" even for languages without a stanza model.
    assert classify_method(content="anything goes", language_code="zz") == "bigram"


@pytestmark_cs
def test_adj_adv_trigram_cs():
    # Trigram containing ADJ + ADV but no NOUN.
    result = classify_method(content="velmi mírná zima", language_code="cs")
    assert result in {"adj_adv_trigram", "noun_adj_adv_trigram", "trigram_with_adj"}


def test_unsupported_language_unigram_returns_none():
    # POS unknown → can't classify a 1-token word.
    assert classify_method(content="word", language_code="zz") is None


def test_empty_content_returns_none():
    assert classify_method(content="", language_code="cs") is None


def test_overlong_content_returns_none():
    assert (
        classify_method(content="four word phrase here", language_code="cs") is None
    )

