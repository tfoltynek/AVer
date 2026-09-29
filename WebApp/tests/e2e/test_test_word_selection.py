"""ML test word selection.

Pins the contract documented in ``core/views/selectors.py``: every MUNI
shape belongs in the test (unigram picks used to be filtered out), at
most one word per paragraph while covering every paragraph that has MUNI
words, preferring words the user has not been tested on.
"""

import datetime

import pytest

from core.models import Document, Language, Paragraph, Word
from core.views import selectors

WORDS_PER_SHAPE = 5

SHAPE_CONTENTS = {
    Word.Shape.UNIGRAM: "day",
    Word.Shape.BIGRAM: "cold day",
    Word.Shape.TRIGRAM: "bright cold day",
}


@pytest.fixture
def muni_analyzed_document(user_factory):
    """A document with 15 MUNI words: 5 unigrams, 5 bigrams, 5 trigrams."""
    uploader = user_factory(email="uploader@example.com")
    language = Language.objects.first()
    document = Document.objects.create(
        file="documents/e2e-word-selection.txt",
        title="Word selection fixture",
        language=language,
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=uploader,
    )
    for shape, content in SHAPE_CONTENTS.items():
        for i in range(WORDS_PER_SHAPE):
            paragraph = Paragraph.objects.create(
                document=document,
                content=f"It was a bright cold day in April ({shape} {i}).",
                language=language,
            )
            Word.objects.create(
                paragraph=paragraph,
                content=content,
                sentence_blanked="It was a <<BLANK>> in April.",
                source=Word.Source.MUNI_API,
                shape=shape,
            )
    return document


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_selection_includes_every_shape_fully(muni_analyzed_document, user_factory):
    user = user_factory(email="taker@example.com")

    words = selectors.pick_test_words(document=muni_analyzed_document, user=user)

    counts = {shape: 0 for shape in Word.Shape}
    for word in words:
        counts[word.shape] += 1
    assert counts == {
        Word.Shape.UNIGRAM: WORDS_PER_SHAPE,
        Word.Shape.BIGRAM: WORDS_PER_SHAPE,
        Word.Shape.TRIGRAM: WORDS_PER_SHAPE,
    }


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_selection_excludes_local_analyzer_words(muni_analyzed_document, user_factory):
    user = user_factory(email="taker@example.com")
    local_word = Word.objects.create(
        paragraph=Paragraph.objects.create(
            document=muni_analyzed_document,
            content="A local-analyzer paragraph.",
            language=muni_analyzed_document.language,
        ),
        content="local",
        source=Word.Source.LOCAL_ANALYZER,
        shape=Word.Shape.UNIGRAM,
    )

    words = selectors.pick_test_words(document=muni_analyzed_document, user=user)

    assert local_word not in words


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_selection_uses_each_paragraph_once(muni_analyzed_document, user_factory):
    user = user_factory(email="taker@example.com")
    # A second word on an existing paragraph; only one of the two may be picked.
    paragraph = Paragraph.objects.filter(document=muni_analyzed_document).first()
    Word.objects.create(
        paragraph=paragraph,
        content="second",
        source=Word.Source.MUNI_API,
        shape=Word.Shape.UNIGRAM,
    )

    words = selectors.pick_test_words(document=muni_analyzed_document, user=user)

    paragraph_ids = [word.paragraph_id for word in words]
    assert len(paragraph_ids) == len(set(paragraph_ids))


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_selection_covers_every_paragraph_with_words(
    muni_analyzed_document, user_factory
):
    user = user_factory(email="taker@example.com")
    for i in range(10):
        paragraph = Paragraph.objects.create(
            document=muni_analyzed_document,
            content=f"Extra unigram paragraph {i}.",
            language=muni_analyzed_document.language,
        )
        Word.objects.create(
            paragraph=paragraph,
            content="extra",
            source=Word.Source.MUNI_API,
            shape=Word.Shape.UNIGRAM,
        )

    words = selectors.pick_test_words(document=muni_analyzed_document, user=user)

    paragraph_count = Paragraph.objects.filter(document=muni_analyzed_document).count()
    assert len(words) == paragraph_count == 25


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_selection_prefers_untested_words(muni_analyzed_document, user_factory):
    from django.utils import timezone

    from core.models import Test, TestedParagraph

    user = user_factory(email="taker@example.com")
    tested = Test.objects.create(
        document=muni_analyzed_document,
        user=user,
        type="authorML",
        submitted_at=timezone.now(),
    )
    already_seen = list(
        Word.objects.filter(
            paragraph__document=muni_analyzed_document, shape=Word.Shape.BIGRAM
        )[:2]
    )
    TestedParagraph.objects.bulk_create(
        [TestedParagraph(test=tested, word=w) for w in already_seen]
    )

    words = selectors.pick_test_words(document=muni_analyzed_document, user=user)

    # Every untested bigram must be picked; tested ones only fill their
    # own paragraphs' slots.
    bigrams = {w.pk for w in words if w.shape == Word.Shape.BIGRAM}
    untested_bigram_ids = set(
        Word.objects.filter(
            paragraph__document=muni_analyzed_document, shape=Word.Shape.BIGRAM
        )
        .exclude(pk__in=[w.pk for w in already_seen])
        .values_list("pk", flat=True)
    )
    assert untested_bigram_ids <= bigrams


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_selection_from_15_words_in_12_paragraphs_yields_12(user_factory):
    """Regression: the old 8-per-shape cap dropped paragraphs whose only
    words belonged to an over-cap shape (reported as an 11-item test from
    a 15-word, 12-paragraph document)."""
    uploader = user_factory(email="uploader@example.com")
    user = user_factory(email="taker@example.com")
    language = Language.objects.first()
    document = Document.objects.create(
        file="documents/e2e-uncapped-selection.txt",
        title="Uncapped selection fixture",
        language=language,
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=uploader,
    )
    paragraphs = [
        Paragraph.objects.create(
            document=document,
            content=f"Uncapped fixture paragraph {i}.",
            language=language,
        )
        for i in range(12)
    ]
    for paragraph in paragraphs[:9]:
        Word.objects.create(
            paragraph=paragraph,
            content="day",
            source=Word.Source.MUNI_API,
            shape=Word.Shape.UNIGRAM,
        )
    for paragraph in paragraphs[9:]:
        Word.objects.create(
            paragraph=paragraph,
            content="cold day",
            source=Word.Source.MUNI_API,
            shape=Word.Shape.BIGRAM,
        )
        Word.objects.create(
            paragraph=paragraph,
            content="bright cold day",
            source=Word.Source.MUNI_API,
            shape=Word.Shape.TRIGRAM,
        )

    words = selectors.pick_test_words(document=document, user=user)

    assert len(words) == 12
    assert {word.paragraph_id for word in words} == {p.pk for p in paragraphs}


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_selection_prefers_untested_word_within_a_paragraph(
    muni_analyzed_document, user_factory
):
    from django.utils import timezone

    from core.models import Test, TestedParagraph

    user = user_factory(email="taker@example.com")
    # Two words share one paragraph; the user has been tested on one of
    # them. The untested sibling must win the paragraph's single slot.
    paragraph = Paragraph.objects.filter(document=muni_analyzed_document).first()
    tested_word = Word.objects.get(paragraph=paragraph)
    untested_word = Word.objects.create(
        paragraph=paragraph,
        content="fresh",
        source=Word.Source.MUNI_API,
        shape=Word.Shape.UNIGRAM,
    )
    tested = Test.objects.create(
        document=muni_analyzed_document,
        user=user,
        type="authorML",
        submitted_at=timezone.now(),
    )
    TestedParagraph.objects.create(test=tested, word=tested_word)

    words = selectors.pick_test_words(document=muni_analyzed_document, user=user)

    picked_for_paragraph = [w for w in words if w.paragraph_id == paragraph.pk]
    assert picked_for_paragraph == [untested_word]
