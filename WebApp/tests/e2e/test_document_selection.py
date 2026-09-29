"""document_for_test eligibility.

Every test a user generates comes from a document they uploaded
themselves. Both branches (never-tested preference and fewest-tests
fallback) must go through the same eligibility rules; the fallback used to
silently drop the completed-AI-analysis filter.
"""

import datetime

import pytest
from django.core.exceptions import BadRequest

from core.models import AnalysisJob, Document, Language
from core.views import selectors


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
def test_returns_the_users_own_analyzed_document(user_factory):
    owner = user_factory(email="owner@example.com")
    document = _make_document(title="analyzed", uploader=owner)

    assert selectors.document_for_test(owner) == document


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_never_returns_someone_elses_document(user_factory):
    stranger = user_factory(email="stranger@example.com")
    owner = user_factory(email="owner@example.com")
    _make_document(title="not-mine", uploader=stranger)

    with pytest.raises(BadRequest):
        selectors.document_for_test(owner)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_fallback_never_returns_unanalyzed_document(user_factory):
    owner = user_factory(email="owner@example.com")
    analyzed = _make_document(title="analyzed", uploader=owner)
    _make_document(title="unanalyzed", uploader=owner, analyzed=False)
    # The owner has already tested every eligible document: force the fallback.
    from core.models import Test

    Test.objects.create(document=analyzed, user=owner, type="authorML")

    assert selectors.document_for_test(owner) == analyzed


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_raises_when_nothing_is_eligible(user_factory):
    owner = user_factory(email="owner@example.com")

    with pytest.raises(BadRequest):
        selectors.document_for_test(owner)
