"""What migration 0029 may and may not relabel.

The migration re-labels MUNI unigrams without `predictions` as randomly
removed words. Its blast radius is the interesting part: local-analyzer
picks, ML unigrams and every multi-word pick have to survive untouched,
because for those shapes a missing `predictions` list does not mean the word
was picked at random.
"""

import importlib

import pytest

from core.models import Document, Language, Paragraph, Word

# A module name starting with a digit cannot be imported with `from … import`.
MIGRATION = importlib.import_module("core.migrations.0029_muni_random_unigrams")


class _Apps:
    """The two-argument shim `RunPython` hands a data migration."""

    @staticmethod
    def get_model(app_label, model_name):
        assert (app_label, model_name) == ("core", "Word")
        return Word


def _word(paragraph, content, *, source, shape, method, predictions):
    return Word.objects.create(
        paragraph=paragraph,
        content=content,
        source=source,
        shape=shape,
        selection_method=method,
        predictions=predictions,
        sentence_blanked=f"a sentence with <<BLANK>> in it ({content})",
        sentence_index=0,
        index=0,
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_only_muni_unigrams_without_predictions_are_relabelled():
    language = Language.objects.get(code="en")
    document = Document.objects.create(
        language=language, file="x.txt", title="x", publication_date="2026-01-01"
    )
    paragraph = Paragraph.objects.create(
        document=document, content="a paragraph", language=language
    )

    ml_unigram = _word(
        paragraph, "April", source="muni_api", shape="unigram",
        method="ml_noun_unigram", predictions={"words": [{"word": "May"}]},
    )
    random_unigram = _word(
        paragraph, "cold", source="muni_api", shape="unigram",
        method="ml_adj_unigram", predictions=None,
    )
    muni_bigram = _word(
        paragraph, "cold day", source="muni_api", shape="bigram",
        method="bigram", predictions=None,
    )
    muni_trigram = _word(
        paragraph, "a bright cold", source="muni_api", shape="trigram",
        method="noun_adj_trigram", predictions=None,
    )
    local_pick = _word(
        paragraph, "clocks", source="local_analyzer", shape="unigram",
        method="most_used_content_word", predictions=None,
    )

    MIGRATION.relabel_random_unigrams(_Apps, None)

    for word in (ml_unigram, random_unigram, muni_bigram, muni_trigram, local_pick):
        word.refresh_from_db()

    # The one row the signal identifies: a MUNI single word the model did
    # not pick.
    assert random_unigram.selection_method == "random"
    # Everything else keeps what it had.
    assert ml_unigram.selection_method == "ml_noun_unigram"
    assert muni_bigram.selection_method == "bigram"
    assert muni_trigram.selection_method == "noun_adj_trigram"
    assert local_pick.selection_method == "most_used_content_word"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_running_twice_changes_nothing_more():
    language = Language.objects.get(code="en")
    document = Document.objects.create(
        language=language, file="y.txt", title="y", publication_date="2026-01-01"
    )
    paragraph = Paragraph.objects.create(
        document=document, content="another paragraph", language=language
    )
    word = _word(
        paragraph, "frost", source="muni_api", shape="unigram",
        method="ml_noun_unigram", predictions=None,
    )

    MIGRATION.relabel_random_unigrams(_Apps, None)
    MIGRATION.relabel_random_unigrams(_Apps, None)

    word.refresh_from_db()
    assert word.selection_method == "random"
