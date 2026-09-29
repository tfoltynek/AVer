from django.core.exceptions import BadRequest
from django.db.models import Count

from core.models import (
    Document,
    Test,
    TestedParagraph,
    User,
    Word,
)

# The MUNI API returns 15 items per analysis (unigrams, bigrams and
# trigrams) and all of them belong in the test, subject to one word per
# paragraph. The source filter keeps local-analyzer picks (also unigrams)
# out.


def pick_test_words(*, document: Document, user: User) -> list[Word]:
    """Pick the words for one ML test.

    One word from every paragraph that has MUNI words, preferring words
    this user has not been tested on for this document. Random within
    those constraints.
    """
    tested_word_ids = set(
        TestedParagraph.objects.filter(test__user=user, test__document=document)
        .exclude(test__submitted_at__isnull=True)
        .values_list("word_id", flat=True)
    )
    candidates = list(
        Word.objects.filter(paragraph__document=document, source=Word.Source.MUNI_API)
        .select_related("paragraph")
        .order_by("?")
    )
    # Stable sort keeps the random order within each group while floating
    # untested words to the front.
    candidates.sort(key=lambda word: word.pk in tested_word_ids)

    picked: list[Word] = []
    used_paragraphs: set[int] = set()
    for word in candidates:
        if word.paragraph.pk in used_paragraphs:
            continue
        picked.append(word)
        used_paragraphs.add(word.paragraph.pk)
    return picked


def eligible_documents(user: User):
    """Documents this user can be tested on: their own uploads with a
    completed AI analysis.

    Every test a regular user generates comes from a document they uploaded
    themselves, so no language-proficiency or academic-field filtering is
    needed: the taker is the author. Both branches of ``document_for_test``
    and the non-admin half of ``documents_for_test_picker`` go through this
    queryset, so no path can hand out someone else's or an unanalyzed
    document.
    """
    return (
        Document.objects.filter(
            uploaded_by=user,
            analysis_jobs__status="completed",
            analysis_jobs__analysis_type="ai",
        )
        .select_related("language")
        .distinct()
        .order_by("title")
    )


def document_for_test(user: User) -> Document:
    """One of the user's own documents for their next test.

    Prefer an eligible document the user has never tested; otherwise the
    eligible document with the fewest tests overall.
    """
    eligible = eligible_documents(user)
    if not eligible.exists():
        raise BadRequest("")

    tested_document_ids = Test.objects.filter(user=user).values_list(
        "document_id", flat=True
    )
    untested = eligible.exclude(pk__in=tested_document_ids)
    if untested.exists():
        chosen = untested.order_by("?").first()
    else:
        chosen = (
            eligible.annotate(test_count=Count("tests")).order_by("test_count").first()
        )
    if chosen is None:
        raise BadRequest("")
    return chosen


def analyzed_documents():
    """Documents an admin may force a test from: completed AI analysis,
    nothing else.
    """
    return (
        Document.objects.filter(
            analysis_jobs__status="completed",
            analysis_jobs__analysis_type="ai",
        )
        .select_related("language")
        .distinct()
        .order_by("title")
    )


def documents_for_test_picker(user: User):
    """The documents this user may pick from on the test-select page: any
    analyzed document for admins, their own for everyone else.

    Both the dropdown contents and ``TestCreateView``'s validation go
    through it, so the list offered and the ids accepted cannot drift apart.
    """
    return analyzed_documents() if user.is_admin else eligible_documents(user)
