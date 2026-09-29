"""Unit tests for reading the MUNI service's selection category.

The class keys come from FI MUNI's README (`WebAPI/cloze_config.py`,
production snapshot 2026-09-22): six real cloze classes plus the synthetic
`random` fallback. All but one map straight onto a calibration bucket;
`trigrams` needs the word's POS tags, because the service has one class for
content-word trigrams where the calibration study had three groups.

Note that the class names describe the group, not the word:
`bigrams_NOUN_ADJ` came back for "milionů korun", two nouns.
"""

import pytest

from core.authorship import SELECTOR_PROBS
from core.utils.muni_category import (
    CATEGORIES,
    POS_DEPENDENT,
    UNMAPPED,
    method_from_category,
)


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        # the service's real classes, bar the one that needs POS tags
        ("unigrams_NOUN", "ml_noun_unigram"),
        ("unigrams_ADJ", "ml_adj_unigram"),
        ("bigrams_NOUN_ADJ", "bigram"),
        ("bigrams_ADV_ADJ", "bigram"),
        ("trigrams_w_ADJ", "trigram_with_adj"),
        ("random", "random"),
        # a class key we do not know, read from the naming scheme instead
        ("trigrams_w_NOUN_ADJ", "noun_adj_trigram"),
        ("trigrams_NOUN_ADJ_ADV", "noun_adj_adv_trigram"),
        ("bigrams_NOUN_NOUN", "bigram"),
        # shape without a group: seen as an origin_category, and unambiguous
        # for bigrams because they share a single bucket
        ("bigrams", "bigram"),
    ],
)
def test_known_scheme_maps_to_a_calibrated_bucket(category, expected):
    method = method_from_category(category)
    assert method == expected
    assert method in SELECTOR_PROBS, "every mapped bucket must be calibrated"


@pytest.mark.parametrize(
    "category",
    ["unigrams_VERB", "unigrams_ADV", "trigrams_w_VERB", "trigrams_NOUN_VERB_DET"],
)
def test_understood_but_uncalibrated_returns_none(category):
    # We can read the category, the study just has no value for it. That is
    # a different situation from not understanding it at all: there is
    # nothing to gain from POS-tagging the word ourselves.
    assert method_from_category(category) is None


@pytest.mark.parametrize(
    "category",
    [
        None,
        "",
        "quadgrams_NOUN_ADJ_ADV_DET",  # a shape we do not know
        "unigrams",  # shape only: noun and adjective unigrams differ
        "unigrams_w",  # the "with" marker but no group after it
        "unigrams_NOUN_ADJ",  # a single word belongs to one group, not two
        "something_else_entirely",
    ],
)
def test_unknown_category_asks_the_caller_to_fall_back(category):
    assert method_from_category(category) is UNMAPPED


def test_unmapped_is_falsy_but_not_none():
    # `is UNMAPPED` is the check callers should use; these two properties
    # exist so a mistaken truthiness test fails safe rather than silently
    # treating it as a bucket name.
    assert not UNMAPPED
    assert UNMAPPED is not None


# ── the content-word trigram class ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("pos_tags", "expected"),
    [
        (["NOUN", "ADV", "ADJ"], "noun_adj_adv_trigram"),
        (["ADJ", "ADV", "ADV"], "adj_adv_trigram"),
        (["ADJ", "NOUN", "NOUN"], "noun_adj_trigram"),
        (["ADJ", "ADJ", "ADJ"], "trigram_with_adj"),
    ],
)
def test_trigrams_class_is_split_by_the_words_own_tags(pos_tags, expected):
    # The service extracts every content-word trigram under one class; the
    # study measured three groups within that material, so the tags decide.
    method = method_from_category("trigrams", pos_tags)
    assert method == expected
    assert method in SELECTOR_PROBS


def test_trigrams_class_without_tags_falls_back_to_tagging():
    # Jobs that predate the `pos_tags` field, per FI MUNI's README.
    assert method_from_category("trigrams") is UNMAPPED
    assert method_from_category("trigrams", []) is UNMAPPED


def test_trigrams_of_nouns_alone_has_no_calibrated_group():
    # NOUN NOUN NOUN is a legal pick for the class but matches none of the
    # study's groups, so it scores with the fallback pair rather than being
    # guessed into one.
    assert method_from_category("trigrams", ["NOUN", "NOUN", "NOUN"]) is None


def test_pos_tags_are_ignored_where_the_class_already_decides():
    # A class that names its group outright is not second-guessed.
    assert method_from_category("unigrams_NOUN", ["ADJ"]) == "ml_noun_unigram"
    assert method_from_category("random", ["NOUN"]) == "random"


# ── the table itself ────────────────────────────────────────────────────────


def test_every_listed_bucket_is_calibrated():
    # A typo in CATEGORIES would otherwise weigh answers with the fallback
    # pair and say nothing about it.
    for category, method in CATEGORIES.items():
        assert method is None or method in SELECTOR_PROBS, category


def test_the_services_class_keys_are_all_accounted_for():
    """The six real classes plus `random`, from FI MUNI's README."""
    documented = {
        "unigrams_NOUN",
        "unigrams_ADJ",
        "bigrams_NOUN_ADJ",
        "bigrams_ADV_ADJ",
        "trigrams",
        "trigrams_w_ADJ",
        "random",
    }
    assert documented == set(CATEGORIES) | POS_DEPENDENT
