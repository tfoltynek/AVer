"""Regression coverage for `_classify_returned_word`.

`shape` must come from token count, never from the presence of a
`predictions` list: the service omits predictions on the words it picks at
random, and labelling by predictions once left completed analyses with zero
test-eligible words ("There are no new words available for a test on this
document").

The calibration bucket comes from the `category` the service reports, with
POS tagging as the fallback for a category we do not map or do not get.
"""

import json
from pathlib import Path

from core import tasks
from core.tasks import _classify_returned_word

PREDICTIONS = [{"word": "January", "probability": -0.8616222739219666}]


def _word(content, predictions=None):
    return {
        "paragraph_id": None,
        "content": content,
        "predictions": predictions,
        "sentence_blanked": "It was a bright cold day in <<BLANK>>.",
        "sentence_index": 0,
        "index": 0,
    }


def test_two_word_pick_with_predictions_is_bigram():
    classified = _classify_returned_word(_word("cold day", PREDICTIONS), "en")
    assert classified["shape"] == "bigram"
    assert classified["source"] == "muni_api"


def test_three_word_pick_with_predictions_is_trigram():
    classified = _classify_returned_word(
        _word("bright cold day", PREDICTIONS), "en"
    )
    assert classified["shape"] == "trigram"
    assert classified["source"] == "muni_api"


def test_single_word_with_predictions_is_unigram():
    classified = _classify_returned_word(_word("day", PREDICTIONS), "en")
    assert classified["shape"] == "unigram"
    assert classified["source"] == "muni_api"


def test_single_word_without_predictions_is_unigram():
    classified = _classify_returned_word(_word("day"), "en")
    assert classified["shape"] == "unigram"
    assert classified["source"] == "muni_api"


def test_four_plus_word_pick_buckets_as_unigram():
    classified = _classify_returned_word(
        _word("it was a bright cold day", PREDICTIONS), "en"
    )
    assert classified["shape"] == "unigram"


# ── calibration bucket from the service's category ──────────────────────────


def _muni_word(content, category=None, origin_category=None, predictions=None):
    word = _word(content, predictions)
    if category is not None:
        word["category"] = category
    if origin_category is not None:
        word["origin_category"] = origin_category
    return word


def _never_tag(**kwargs):
    raise AssertionError("POS tagging must not run when the category is known")


def test_category_decides_the_bucket_without_pos_tagging(monkeypatch):
    # `classify_method` is imported inside the function, so the patch has to
    # land on the module that defines it, not on core.tasks.
    monkeypatch.setattr("core.utils.pos_method.classify_method", _never_tag)

    classified = _classify_returned_word(_muni_word("duby", "unigrams_NOUN"), "cs")

    assert classified["selection_method"] == "ml_noun_unigram"
    assert classified["muni_category"] == "unigrams_NOUN"
    assert classified["muni_origin_category"] is None


def test_random_pick_keeps_both_service_fields(monkeypatch):
    monkeypatch.setattr("core.utils.pos_method.classify_method", _never_tag)

    classified = _classify_returned_word(
        _muni_word("oblohu", "random", origin_category="unigrams_NOUN"), "cs"
    )

    # A word the service removed at random is calibrated as such, never as
    # the ML noun unigram stanza would have made of it.
    assert classified["selection_method"] == "random"
    assert classified["muni_category"] == "random"
    assert classified["muni_origin_category"] == "unigrams_NOUN"


def test_missing_category_falls_back_to_pos_tagging(monkeypatch):
    calls = []

    def fake(*, content, language_code):
        calls.append((content, language_code))
        return "ml_adj_unigram"

    monkeypatch.setattr("core.utils.pos_method.classify_method", fake)

    classified = _classify_returned_word(_word("mírná"), "cs")

    assert calls == [("mírná", "cs")]
    assert classified["selection_method"] == "ml_adj_unigram"
    assert classified["muni_category"] is None


def test_unknown_category_warns_and_falls_back(monkeypatch):
    monkeypatch.setattr(
        "core.utils.pos_method.classify_method",
        lambda **kwargs: "ml_noun_unigram",
    )
    # settings.LOGGING gives the "core" logger propagate=False, so caplog
    # would never see the record; the suite mocks the module logger instead.
    warnings = []
    monkeypatch.setattr(
        tasks.logger, "warning", lambda msg, *args: warnings.append(msg % args)
    )

    classified = _classify_returned_word(
        _muni_word("duby", "quadgrams_NOUN_ADJ_ADV_DET"), "cs"
    )

    # The raw value is kept so the admin shows what the service actually
    # said, and the log is how a new category reaches us.
    assert classified["selection_method"] == "ml_noun_unigram"
    assert classified["muni_category"] == "quadgrams_NOUN_ADJ_ADV_DET"
    assert len(warnings) == 1
    assert "unmapped MUNI category 'quadgrams_NOUN_ADJ_ADV_DET'" in warnings[0]


def test_failing_pos_tagger_is_not_fatal(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("stanza is unavailable")

    monkeypatch.setattr("core.utils.pos_method.classify_method", boom)

    classified = _classify_returned_word(_word("cokoliv"), "cs")

    assert classified["selection_method"] is None
    assert classified["shape"] == "unigram"


def test_a_real_service_result_classifies_end_to_end():
    """The recorded answer to a real job, classified word by word.

    Kept as a file so the assertions below are about a payload the service
    actually sent (job ebe05152…, two paragraphs of public-domain Czech,
    2026-09-20) rather than about a dict written to match the code. Three of
    its four words were removed at random, which is the case that used to be
    scored as if the model had chosen them.
    """
    payload = json.loads(
        (Path(__file__).parent / "data/muni_result_with_category.json").read_text(
            encoding="utf-8"
        )
    )

    classified = [_classify_returned_word(w, "cs") for w in payload["words"]]
    by_content = {c["content"]: c for c in classified}

    assert by_content["Duch"]["selection_method"] == "ml_noun_unigram"
    for content in ("oblohu", "světlo", "dobré"):
        assert by_content[content]["selection_method"] == "random", content
        assert by_content[content]["muni_category"] == "random"
    # The class a random word would otherwise have had is kept but does not
    # decide its weight.
    assert by_content["dobré"]["muni_origin_category"] == "unigrams_ADJ"
    assert all(c["source"] == "muni_api" for c in classified)


def test_content_word_trigram_uses_the_tags_the_service_sent(monkeypatch):
    monkeypatch.setattr("core.utils.pos_method.classify_method", _never_tag)

    word = _muni_word("praví staré písemnosti", "trigrams")
    word["pos_tags"] = "VERB ADJ NOUN"
    classified = _classify_returned_word(word, "cs")

    assert classified["selection_method"] == "noun_adj_trigram"
    assert classified["muni_category"] == "trigrams"
    assert classified["muni_pos_tags"] == "VERB ADJ NOUN"


def test_content_word_trigram_without_tags_falls_back(monkeypatch):
    # Words ingested before the service reported `pos_tags`.
    monkeypatch.setattr(
        "core.utils.pos_method.classify_method",
        lambda **kwargs: "adj_adv_trigram",
    )

    classified = _classify_returned_word(_muni_word("a b c", "trigrams"), "cs")

    assert classified["selection_method"] == "adj_adv_trigram"
    assert classified["muni_pos_tags"] is None
