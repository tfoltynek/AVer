"""E2E coverage for the failed-analysis branch.

`start_analysis` marks the `AnalysisJob` as 'failed' when every MUNI attempt
fails. These tests inject a `FakeMuniClient` at the client seam
(`core.tasks.get_muni_client`) instead of monkeypatching HTTP internals:

  upload → run the analysis task inline → expect failed status + failure UI.

The Celery broker is in-memory in test runs (no worker), so we invoke
`start_analysis` directly in-process instead of going through `.delay()`.
Protocol-level failure behaviour (Retry-After, deadlines, HTML 404 bodies)
lives in `test_muni_async_contract.py` against a real local HTTP server.
"""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from core.muni import FakeAttempt, FakeMuniClient
from tests.e2e.pages import DocumentListPage, DocumentUploadPage

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_FILE = FIXTURES / "sample.txt"


def _upload_sample_document(page, live_server, user_factory, login):
    user = user_factory(email="failed-ai@example.com")
    login(user)
    upload = DocumentUploadPage(page, live_server.url)
    upload.visit()
    upload.fill_and_submit(file_path=SAMPLE_FILE)
    page.wait_for_url(f"{live_server.url}/dashboard")
    return user


def _install_client(monkeypatch, client):
    import core.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "get_muni_client", lambda: client)
    return client


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_failed_ai_analysis_marks_job_failed_and_shows_failure_ui(
    live_server, page, user_factory, login, monkeypatch
):
    """Every MUNI attempt fails → analysis job goes to 'failed' → list page
    shows the Failed badge and swaps the 'Start test' CTA for 'Delete
    document'."""
    _install_client(
        monkeypatch,
        FakeMuniClient([
            FakeAttempt(words=None, failures=[("http_status", "MUNI submit returned HTTP 500")])
        ]),
    )

    user = _upload_sample_document(page, live_server, user_factory, login)

    from core.models import Document
    from core.tasks import start_analysis

    document = Document.objects.get(uploaded_by=user)
    start_analysis(document.current_analysis.pk)

    document.refresh_from_db()
    assert document.current_analysis.status == "failed", (
        f"expected 'failed', got {document.current_analysis.status!r}"
    )

    list_page = DocumentListPage(page, live_server.url)
    list_page.visit()
    card = page.locator(".DocumentCard").first
    expect(card.locator(".Badge--danger")).to_be_visible()
    expect(card.locator(".Button--danger:has-text('Delete document')")).to_be_visible()
    expect(card.locator(".Button:has-text('Start test')")).to_have_count(0)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_failed_ai_analysis_blocks_test_creation(
    live_server, page, user_factory, login, monkeypatch
):
    """`CreateDocumentTestView` redirects back to /dashboard if the AI job
    isn't 'completed' — even when it has explicitly 'failed'."""
    _install_client(monkeypatch, FakeMuniClient())

    user = _upload_sample_document(page, live_server, user_factory, login)

    from core.models import Document
    from core.tasks import start_analysis

    document = Document.objects.get(uploaded_by=user)
    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()
    assert document.current_analysis.status == "failed"

    page.goto(f"{live_server.url}/documents/{document.pk}/test")
    expect(page).to_have_url(f"{live_server.url}/dashboard")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_failed_ai_analysis_leaves_no_partial_words(
    pending_ai_document, monkeypatch
):
    """A failed run must not have persisted partial Words, and the failure
    events land in AnalysisFailureLog under the attempt that emitted them."""
    _install_client(
        monkeypatch,
        FakeMuniClient([
            FakeAttempt(
                words=None,
                failures=[("network_error", "ConnectionError: simulated network drop")],
            )
        ]),
    )

    from core.models import AnalysisFailureLog, Word
    from core.tasks import start_analysis

    document = pending_ai_document
    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()

    assert document.current_analysis.status == "failed"
    assert (
        Word.objects.filter(paragraph__document=document).count() == 0
    ), "failed analysis leaked partial Word rows"
    kinds = set(
        AnalysisFailureLog.objects.filter(
            analysis_job=document.current_analysis
        ).values_list("kind", flat=True)
    )
    assert "network_error" in kinds


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_partial_failure_commits_what_we_got(pending_ai_document, monkeypatch):
    """If some MUNI attempts fail but at least one finishes, the analysis is
    completed with the words from the successful attempt — we don't lose the
    whole run to a transient outage."""
    import core.tasks as tasks_module
    from core.models import Paragraph, Word
    from core.tasks import start_analysis

    # Partial success needs several attempts; production default is 1.
    monkeypatch.setattr(tasks_module, "MUNI_API_CALLS", 5)

    document = pending_ai_document
    paragraph = Paragraph.objects.filter(document=document).first()
    good_word = {
        "paragraph_id": paragraph.pk,
        "content": "test word",
        "predictions": None,
        "sentence_blanked": "this is a <<BLANK>>.",
        "sentence_index": 0,
        "index": 4,
    }

    client = _install_client(
        monkeypatch,
        FakeMuniClient([
            FakeAttempt(words=None, failures=[("job_failed", "MUNI job failed: simulated")]),
            FakeAttempt(words=None, failures=[("job_failed", "MUNI job failed: simulated")]),
            FakeAttempt(words=[good_word]),
            FakeAttempt(words=None, failures=[("job_failed", "MUNI job failed: simulated")]),
            FakeAttempt(words=None, failures=[("job_failed", "MUNI job failed: simulated")]),
        ]),
    )

    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()

    # One of the five attempts finished → job is completed, not failed.
    assert document.current_analysis.status == "completed", (
        f"expected 'completed' from partial success, got {document.current_analysis.status!r}"
    )
    assert client.attempts_run == 5
    # And the one word from the finished attempt landed in the DB.
    assert Word.objects.filter(paragraph__document=document).count() >= 1


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_failed_analysis_delete_removes_document(
    live_server, page, user_factory, login, monkeypatch
):
    """The recovery path: click the Delete-document button on a failed card
    and confirm. The document row disappears and is gone from the DB."""
    _install_client(monkeypatch, FakeMuniClient())

    user = _upload_sample_document(page, live_server, user_factory, login)

    from core.models import Document
    from core.tasks import start_analysis

    document = Document.objects.get(uploaded_by=user)
    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()
    assert document.current_analysis.status == "failed"

    list_page = DocumentListPage(page, live_server.url)
    list_page.visit()
    expect(page.locator(".DocumentCard")).to_have_count(1)

    page.click(".DocumentCard .Button--danger:has-text('Delete document')")
    page.wait_for_selector("#dialog #dialog-confirm")
    # The confirm button POSTs via htmx and swaps #document-list with the
    # post-delete list (no full-page navigation); wait for that response.
    with page.expect_response(
        lambda r: r.url.endswith(f"/documents/{document.pk}/delete")
        and r.request.method == "POST"
    ):
        page.click("#dialog #dialog-confirm")
    page.wait_for_load_state("networkidle")

    expect(page.locator(".DocumentCard")).to_have_count(0)
    assert not Document.objects.filter(pk=document.pk).exists()
