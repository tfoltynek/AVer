"""Map the MUNI service's selection category onto a calibration bucket.

Every word the async API returns carries a `category` saying how it was
chosen, and randomly removed words additionally carry an `origin_category`
naming the class the word would otherwise have belonged to.

`CATEGORIES` below lists every value we know, and is the place to edit when
FI MUNI confirms another one. The values in it were measured, not guessed:
345 words across 37 documents in cs, sk and en (2026-09-20 and 2026-09-21),
with no new value after the tenth document. What the service has never sent
is marked as such — three of the study's four trigram groups are still
unseen, so their names follow the pattern of the one that was returned.

The naming scheme is `<shape>s[_w][_<POS>…]`, and the parts of speech name
the *group*, not the word: `bigrams_NOUN_ADJ` came back for "milionů korun",
which is two nouns, and `w` stands for "with", as in the study's "trigrams
w/ adjectives". `read_scheme` applies that pattern to anything the table
does not hold, so a spelling variant still lands in the right bucket
instead of falling through. The rules deliberately mirror
`core.utils.pos_method.classify_method`, which does the same job from
stanza tags whenever the service tells us nothing.

Three outcomes, and the difference matters:

* a bucket name — the word is calibrated, score it with that pA/pN;
* ``None`` — we understood the category but the calibration study has no
  value for that combination (a verb unigram, say), so scoring falls back
  to `authorship.FALLBACK_PROBS`;
* ``UNMAPPED`` — the category is missing or fits no pattern, so the caller
  should POS-tag the word itself and log that we met something new.
"""

from __future__ import annotations

from typing import Final

_NOUN: Final = "NOUN"
_ADJ: Final = "ADJ"
_ADV: Final = "ADV"

# The service's own name for a word it removed at random rather than by
# model. It happens to equal the local analyzer's strategy name, and both
# mean the same thing, so both score with SELECTOR_PROBS["random"].
RANDOM_CATEGORY: Final = "random"


class _Unmapped:
    """Sentinel: this category tells us nothing we can use."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNMAPPED"

    def __bool__(self) -> bool:
        return False


UNMAPPED: Final = _Unmapped()

# Shape prefixes the service uses. Anything else is a shape we do not know
# how to score.
_SHAPES: Final[frozenset[str]] = frozenset({"unigrams", "bigrams", "trigrams"})

# Every cloze class the service can report, from FI MUNI's own README
# (`WebAPI/cloze_config.py`, production snapshot 2026-09-22), with the
# calibration bucket each one scores in. Six real classes plus the synthetic
# `random` fallback; the quotas in the comments sum to the 15 words a
# document comes back with.
#
# TO ADD ONE: put the service's exact class key on the left and the matching
# key of `authorship.SELECTOR_PROBS` on the right. A unit test checks that
# every bucket named here is one that table actually calibrates, so a typo
# fails the suite rather than quietly weighing answers with a fallback.
# `None` would mean "a class the study never calibrated"; there is none.
CATEGORIES: Final[dict[str, str | None]] = {
    "unigrams_NOUN": "ml_noun_unigram",  # quota 4
    "unigrams_ADJ": "ml_adj_unigram",  # quota 4
    "bigrams_NOUN_ADJ": "bigram",  # quota 2
    "bigrams_ADV_ADJ": "bigram",  # quota 1
    "trigrams_w_ADJ": "trigram_with_adj",  # quota 2
    RANDOM_CATEGORY: RANDOM_CATEGORY,  # fallback, no quota of its own
    # "trigrams" (quota 2) is deliberately absent: see POS_DEPENDENT.
}

# The service has a single class for trigrams built only of content words
# (every token NOUN, ADV or ADJ), while the calibration study split exactly
# that material into three groups by which of those tags actually occur. The
# class name therefore cannot settle the bucket on its own — the word's
# `pos_tags` can, and the service sends them.
POS_DEPENDENT: Final[frozenset[str]] = frozenset({"trigrams"})


def bucket_from_pos(pos_tags: list[str] | tuple[str, ...]) -> str | None:
    """The calibration group for a trigram, from the tags of its tokens.

    The same rules `core.utils.pos_method` applies to stanza's output, so
    both routes agree about what a group is.
    """
    tag_set = set(pos_tags)
    if {_NOUN, _ADJ, _ADV} <= tag_set:
        return "noun_adj_adv_trigram"
    if {_ADJ, _ADV} <= tag_set:
        return "adj_adv_trigram"
    if {_NOUN, _ADJ} <= tag_set:
        return "noun_adj_trigram"
    if _ADJ in tag_set:
        return "trigram_with_adj"
    return None


def method_from_category(
    category: str | None,
    pos_tags: list[str] | tuple[str, ...] | None = None,
) -> str | None | _Unmapped:
    """Return the `selection_method` for a service category.

    Looks the category up in `CATEGORIES`, consults `pos_tags` for the one
    class that needs them, and falls back to reading the naming scheme for
    anything not listed.

    >>> method_from_category("unigrams_NOUN")
    'ml_noun_unigram'
    >>> method_from_category("bigrams_ADV_ADJ")
    'bigram'
    >>> method_from_category("trigrams_w_ADJ")
    'trigram_with_adj'
    >>> method_from_category("random")
    'random'
    >>> method_from_category("trigrams", ["ADJ", "NOUN", "NOUN"])
    'noun_adj_trigram'
    >>> method_from_category(None) is UNMAPPED
    True
    """
    if not category:
        return UNMAPPED
    if category in POS_DEPENDENT:
        # Without the tags there is nothing to choose between the three
        # content-word trigram groups; let the caller tag the word.
        return bucket_from_pos(pos_tags) if pos_tags else UNMAPPED
    if category in CATEGORIES:
        return CATEGORIES[category]
    return read_scheme(category)


def read_scheme(category: str) -> str | None | _Unmapped:
    """Work a category out from its name, for values not in `CATEGORIES`.

    Same rules, applied to the pattern rather than to a known string, so a
    group the service adds or spells differently is still placed.
    """
    if category == RANDOM_CATEGORY:
        return RANDOM_CATEGORY

    prefix, _, rest = category.partition("_")
    if prefix not in _SHAPES:
        return UNMAPPED

    if prefix == "bigrams":
        # The study found the POS sub-strata of bigrams too close to split,
        # so every bigram group shares one bucket — including a bare
        # "bigrams" with no group named at all.
        return "bigram"

    # "w" is the study's "with", not a part of speech.
    tags = [tag for tag in rest.split("_") if tag and tag != "w"]
    if not tags:
        # Shape only. Which unigram or trigram bucket it is depends on the
        # parts of speech, so the caller has to tag the word itself.
        return UNMAPPED

    if prefix == "unigrams":
        # Only nouns and adjectives were calibrated as single words, and a
        # single word belongs to exactly one of them.
        if len(tags) != 1:
            return UNMAPPED
        if tags[0] == _NOUN:
            return "ml_noun_unigram"
        if tags[0] == _ADJ:
            return "ml_adj_unigram"
        return None

    return bucket_from_pos(tags)
