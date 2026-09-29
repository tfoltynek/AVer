"""Robustness when a document/job is deleted while its AI analysis is in flight.

If a user deletes a document mid-analysis, its `AnalysisJob` is cascade-deleted.
The still-running analysis must then:

  1. stop promptly (raise ``AnalysisAborted``) instead of polling MUNI to the
     per-job deadline for a document that no longer exists, and
  2. not emit a traceback when it tries to record a failure against the missing
     job — ``_log_failure`` should skip it with a single quiet log line.

Both were observed live: deleting two documents mid-analysis left the worker
polling MUNI for an hour and spamming ``FOREIGN KEY constraint failed``
tracebacks. The abort check enters the client through ``check_abort``;
`test_muni_async_contract.py` pins that `HttpMuniClient` runs it on every
polling round. These tests pin the orchestration side at the client seam.
"""

from unittest.mock import MagicMock

import pytest

from core.muni import FakeAttempt, FakeMuniClient


def _ai_job(document):
    from core.models import AnalysisJob

    return AnalysisJob.objects.filter(document=document, analysis_type="ai").first()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_log_failure_skips_deleted_job_without_traceback(
    pending_ai_document, monkeypatch
):
    """_log_failure must tolerate a parent job that was deleted mid-analysis:
    no row, no exception, and a quiet warning rather than a traceback."""
    import core.tasks as tasks_module
    from core.models import AnalysisFailureLog, AnalysisJob
    from core.tasks import _log_failure

    job = _ai_job(pending_ai_document)
    stale = AnalysisJob.objects.get(pk=job.pk)  # in-memory handle
    AnalysisJob.objects.filter(pk=job.pk).delete()  # simulate cascade delete

    mock_logger = MagicMock()
    monkeypatch.setattr(tasks_module, "logger", mock_logger)

    # Must not raise.
    _log_failure(stale, 1, "deadline_exceeded", "boom", phase="poll")

    assert AnalysisFailureLog.objects.count() == 0
    assert mock_logger.exception.call_count == 0, (
        "a deleted parent job is expected; it must not log a traceback"
    )
    assert mock_logger.warning.called, "should note the skip with one warning line"


def _client_that_deletes_the_job(job_pk):
    """A FakeMuniClient whose first attempt deletes the AnalysisJob before the
    abort check runs — the seam-level equivalent of a mid-poll deletion."""
    from core.models import AnalysisJob

    def delete_job():
        AnalysisJob.objects.filter(pk=job_pk).delete()

    return FakeMuniClient([FakeAttempt(before=delete_job)])


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_analysis_aborts_when_job_deleted_midway(pending_ai_document):
    """perform_ai_analysis must raise AnalysisAborted when its job disappears,
    instead of carrying on to the deadline."""
    from core.models import Paragraph
    from core.tasks import AnalysisAborted, perform_ai_analysis

    document = pending_ai_document
    paragraphs = list(Paragraph.objects.filter(document=document))
    job = _ai_job(document)
    client = _client_that_deletes_the_job(job.pk)

    with pytest.raises(AnalysisAborted):
        perform_ai_analysis(document, paragraphs, job=job, client=client)

    assert client.attempts_run == 1, "the abort must stop the run at once"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_start_analysis_survives_job_deleted_midway(
    pending_ai_document, monkeypatch
):
    """start_analysis must swallow the mid-analysis deletion: no exception
    propagates, no Words are written, and it stops early."""
    import core.tasks as tasks_module
    from core.models import Word
    from core.tasks import start_analysis

    document = pending_ai_document
    job = _ai_job(document)
    client = _client_that_deletes_the_job(job.pk)
    monkeypatch.setattr(tasks_module, "get_muni_client", lambda: client)

    start_analysis(job.pk)  # must not raise

    assert Word.objects.filter(paragraph__document=document).count() == 0
    assert client.attempts_run == 1, "the abort must stop the run at once"
