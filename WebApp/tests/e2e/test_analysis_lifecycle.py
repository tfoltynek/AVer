"""AnalysisJob lifecycle interface.

Status and timestamps move together through mark_running/mark_completed/
mark_failed; "which analysis counts" has exactly one definition
(`AnalysisJob.objects.current_for`), and `Document.analysis_state` is the
one value views and templates read.
"""

import datetime

import pytest

from core.models import AnalysisJob, Document, Language


@pytest.fixture
def document(user_factory):
    owner = user_factory(email="lifecycle@example.com")
    return Document.objects.create(
        file="documents/lifecycle.txt",
        title="Lifecycle fixture",
        language=Language.objects.first(),
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=owner,
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_mark_running_stamps_status_and_start(document):
    job = AnalysisJob.objects.create(document=document, analysis_type="ai")

    job.mark_running()
    job.refresh_from_db()

    assert job.status == "running"
    assert job.started_at is not None
    assert job.finished_at is None


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_terminal_transitions_stamp_finish(document):
    completed = AnalysisJob.objects.create(document=document, analysis_type="ai")
    completed.mark_running()
    completed.mark_completed()
    completed.refresh_from_db()
    assert completed.status == "completed"
    assert completed.finished_at is not None
    assert completed.finished_at >= completed.started_at
    assert completed.duration is not None

    failed = AnalysisJob.objects.create(document=document, analysis_type="ai")
    failed.mark_running()
    failed.mark_failed()
    failed.refresh_from_db()
    assert failed.status == "failed"
    assert failed.finished_at is not None


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_current_for_returns_newest_job_of_requested_type(document):
    old = AnalysisJob.objects.create(document=document, analysis_type="ai")
    # created_at is auto_now_add; force a distinct, older value.
    AnalysisJob.objects.filter(pk=old.pk).update(
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    )
    newest = AnalysisJob.objects.create(document=document, analysis_type="ai")
    AnalysisJob.objects.create(document=document, analysis_type="basic")

    assert AnalysisJob.objects.current_for(document, "ai") == newest


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_analysis_state_reads_the_current_job(document):
    assert document.analysis_state == ""

    job = AnalysisJob.objects.create(document=document, analysis_type="ai")
    assert document.analysis_state == "pending"

    job.mark_running()
    assert document.analysis_state == "running"

    job.mark_completed()
    assert document.analysis_state == "completed"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_request_analysis_creates_pending_job(document):
    from core.tasks import request_analysis

    job = request_analysis(document, "ai")

    assert job.pk is not None
    assert job.status == "pending"
    assert job.analysis_type == "ai"
    assert document.current_analysis == job
