"""The success estimate is a 0–100 slider; 0 is a valid answer.

The wizard decides whether to show the estimate page by looking at the
stored value, so an estimate of 0 must count as "answered" or the test
would re-open the estimate page forever after being submitted.
"""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone


def _answered_test(user):
    from core.models import Document, Language, Paragraph, Test, TestedParagraph, Word

    language = Language.objects.filter(code="en").first() or Language.objects.first()
    document = Document.objects.create(
        file="documents/estimate.txt",
        title="Estimate fixture",
        language=language,
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=user,
    )
    paragraph = Paragraph.objects.create(
        document=document, content="A sentence with a gap in it.", language=language
    )
    word = Word.objects.create(
        paragraph=paragraph,
        content="gap",
        sentence_blanked="A sentence with a <<BLANK>> in it.",
        sentence_index=0,
        index=4,
        source=Word.Source.MUNI_API,
        shape=Word.Shape.UNIGRAM,
        selection_method="ml_noun_unigram",
    )
    test = Test.objects.create(document=document, user=user, type="authorML")
    TestedParagraph.objects.create(
        test=test,
        word=word,
        answered_word="gap",
        answer_started_at=timezone.now(),
        answer_ended_at=timezone.now(),
        computed_score=1.0,
        computed_grade="correct",
    )
    return test


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_success_estimate_of_zero_submits_the_test(client, user_factory):
    user = user_factory(email="estimate-zero@example.com")
    client.force_login(user)
    test = _answered_test(user)

    response = client.post(
        reverse("test-success-estimate", args=[test.pk]), {"success_estimate": 0}
    )
    assert response.status_code == 302

    test.refresh_from_db()
    assert test.success_estimate == 0
    assert test.submitted_at is not None

    response = client.get(reverse("document-test", args=[test.pk]))
    assert response.status_code == 200
    used = [t.name for t in response.templates]
    assert "core/document_test_success_estimate.html" not in used, (
        "an estimate of 0 must not re-open the estimate page"
    )
