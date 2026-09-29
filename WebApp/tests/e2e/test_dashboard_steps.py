"""The three-step guide on the dashboard.

Upload a document, generate a test from it, fill it in. Every step counts
only the user's own documents and tests, and step 2 unlocks on the same
queryset that fills the test-select picker.
"""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AnalysisJob, Document, Language, Test


def _make_document(*, title, uploader, analyzed=True):
    document = Document.objects.create(
        file=f"documents/{title}.txt",
        title=title,
        language=Language.objects.filter(code="en").first(),
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=uploader,
    )
    if analyzed:
        AnalysisJob.objects.create(
            document=document, analysis_type="ai", status="completed"
        )
    return document


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_first_visit_starts_at_the_upload_step(client, user_factory):
    user = user_factory(email="fresh@example.com")
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.context["document"] is None
    assert response.context["has_analyzed_document"] is False
    assert response.context["steps_done"] == 0
    # Step 2 stays locked, so its call to action is not offered yet.
    assert reverse("test-select") not in response.content.decode()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_unanalyzed_document_keeps_the_generate_step_locked(client, user_factory):
    user = user_factory(email="waiting@example.com")
    _make_document(title="still-running", uploader=user, analyzed=False)
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.context["has_analyzed_document"] is False
    assert response.context["steps_done"] == 1
    assert reverse("test-select") not in response.content.decode()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_analyzed_document_unlocks_the_generate_step(client, user_factory):
    user = user_factory(email="ready@example.com")
    _make_document(title="analyzed", uploader=user)
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.context["has_analyzed_document"] is True
    assert response.context["generated_test_count"] == 0
    assert response.context["steps_done"] == 1
    assert reverse("test-select") in response.content.decode()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_someone_elses_document_unlocks_nothing(client, user_factory):
    stranger = user_factory(email="stranger@example.com")
    user = user_factory(email="empty-handed@example.com")
    _make_document(title="not-mine", uploader=stranger)
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.context["has_analyzed_document"] is False
    assert response.context["steps_done"] == 0


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_generated_test_advances_to_the_fill_step(client, user_factory):
    user = user_factory(email="generated@example.com")
    document = _make_document(title="analyzed", uploader=user)
    test = Test.objects.create(document=document, user=user, type="authorML")
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.context["generated_test_count"] == 1
    assert response.context["submitted_test_count"] == 0
    assert response.context["pending_test"] == test
    assert response.context["steps_done"] == 2
    assert reverse("document-test", args=[test.pk]) in response.content.decode()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_submitted_test_completes_the_guide(client, user_factory):
    user = user_factory(email="finished@example.com")
    document = _make_document(title="analyzed", uploader=user)
    Test.objects.create(
        document=document,
        user=user,
        type="authorML",
        submitted_at=timezone.now(),
    )
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.context["submitted_test_count"] == 1
    assert response.context["pending_test"] is None
    assert response.context["steps_done"] == 3


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_test_on_someone_elses_document_does_not_count(client, user_factory):
    stranger = user_factory(email="stranger@example.com")
    user = user_factory(email="borrower@example.com")
    document = _make_document(title="not-mine", uploader=stranger)
    Test.objects.create(
        document=document,
        user=user,
        type="previewML",
        submitted_at=timezone.now(),
    )
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.context["generated_test_count"] == 0
    assert response.context["submitted_test_count"] == 0
    assert response.context["steps_done"] == 0
