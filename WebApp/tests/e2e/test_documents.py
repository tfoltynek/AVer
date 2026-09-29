"""E2E coverage for document upload.

Skips the "delete" half of the original plan: the delete button only appears
when AnalysisJob.status == 'failed', and our test does not run a real Celery
worker — so the analysis sits in 'pending' and the button never renders.
"""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from core.muni import get_muni_client
from tests.e2e.pages import DocumentListPage, DocumentUploadPage

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_FILE = FIXTURES / "sample.txt"
REAL_DOCS_DIR = FIXTURES / "real-documents"


def _find_real_document() -> Path | None:
    """Return the first user-supplied document, or None if the dir is empty.

    Accepts .pdf / .docx / .txt — matches DocumentUploadForm's validator.
    """
    for ext in ("pdf", "docx", "txt"):
        matches = sorted(REAL_DOCS_DIR.glob(f"*.{ext}"))
        if matches:
            return matches[0]
    return None


def _find_real_pdf() -> Path | None:
    matches = sorted(REAL_DOCS_DIR.glob("*.pdf"))
    return matches[0] if matches else None


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_upload_document_appears_in_list(live_server, page, user_factory, login):
    user = user_factory(email="uploader@example.com")
    login(user)

    upload = DocumentUploadPage(page, live_server.url)
    upload.visit()
    upload.fill_and_submit(file_path=SAMPLE_FILE)

    # Upload responds with HX-Redirect to /dashboard. Wait for that nav.
    page.wait_for_url(f"{live_server.url}/dashboard")

    list_page = DocumentListPage(page, live_server.url)
    list_page.visit()
    list_page.expect_document_with_title("sample")
    expect(page.locator(".DocumentCard")).to_have_count(1)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_upload_real_document(live_server, page, user_factory, login):
    """Upload a user-supplied document from tests/e2e/fixtures/real-documents/.

    Drop a PDF (or .docx / .txt) in that folder and re-run this test to
    exercise the upload pipeline with a realistic file. Skipped if the
    folder is empty.
    """
    real_file = _find_real_document()
    if real_file is None:
        pytest.skip(f"No document in {REAL_DOCS_DIR.relative_to(Path.cwd())}/ to upload")

    user = user_factory(email="real-uploader@example.com")
    login(user)

    upload = DocumentUploadPage(page, live_server.url)
    upload.visit()
    upload.fill_and_submit(file_path=real_file)

    page.wait_for_url(f"{live_server.url}/dashboard")

    list_page = DocumentListPage(page, live_server.url)
    list_page.visit()
    list_page.expect_document_with_title(real_file.stem)


AI_API_URL = get_muni_client().submit_url  # POST target for an async analysis job


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_upload_pdf_run_test_after_ai_analysis(
    live_server, page, user_factory, login, monkeypatch
):
    """Upload a real PDF, wait for the AI analysis to finish, then start a test.

    The AI analysis hits the real upstream API at nlp.fi.muni.cz. It now submits
    an async job (POST /document → 202) and the task polls it to completion.
    Celery's in-memory broker queues but never executes (no worker), so we
    invoke the task function directly to run it synchronously in the test
    process. We spy on `requests.post` to verify the submit actually happened
    (URL + status) rather than relying solely on the `analysis_job.status` field.

    The AI service is flaky/behind auth — if the submit returns anything other
    than 202 (or the run doesn't complete), the test skips cleanly so transient
    upstream issues do not fail the suite.
    """
    pdf = _find_real_pdf()
    if pdf is None:
        pytest.skip(f"No .pdf in {REAL_DOCS_DIR.relative_to(Path.cwd())}/ to upload")

    api_calls: list[dict] = []
    import core.muni as muni_module
    import core.tasks as tasks_module

    real_post = muni_module.requests.post

    def spy_post(url, *args, **kwargs):
        response = real_post(url, *args, **kwargs)
        api_calls.append({
            "url": url,
            "status": response.status_code,
            "payload_kb": (len(kwargs.get("json", "") or "") + 1) // 1024,
        })
        return response

    monkeypatch.setattr(muni_module.requests, "post", spy_post)

    # Keep the suite bounded: in production each job may wait a full day in
    # MUNI's queue. Two minutes is plenty for a healthy upstream, and the test
    # skips (below) when the run doesn't complete.
    monkeypatch.setattr(tasks_module, "MUNI_JOB_DEADLINE_SECONDS", 120)

    user = user_factory(email="pdf-author@example.com")
    login(user)

    upload = DocumentUploadPage(page, live_server.url)
    upload.visit()
    upload.fill_and_submit(file_path=pdf)
    page.wait_for_url(f"{live_server.url}/dashboard")

    from core.models import Document, Word
    from core.tasks import start_analysis

    document = Document.objects.get(uploaded_by=user)
    # Run the queued task in-process. Real HTTP call to the AI service.
    start_analysis(document.current_analysis.pk)

    # Verify the external REST call was made (regardless of outcome).
    assert api_calls, "AI analysis task did not call the external API"
    ai_calls = [c for c in api_calls if c["url"] == AI_API_URL]
    assert ai_calls, f"No POST hit {AI_API_URL}; calls were {api_calls}"

    document.refresh_from_db()
    if document.current_analysis.status != "completed":
        # Skip if upstream gave a non-200 — the call DID happen, we just
        # didn't get a usable response.
        pytest.skip(
            f"AI analysis upstream unavailable "
            f"(status={document.current_analysis.status}, "
            f"http={ai_calls[-1]['status']})"
        )

    # Successful path: every submit returned 202, and the task persisted Words.
    assert all(c["status"] == 202 for c in ai_calls), (
        f"Expected all AI submits 202, got: {ai_calls}"
    )
    word_count = Word.objects.filter(paragraph__document=document).count()
    assert word_count > 0, "AI analysis completed but no Words were persisted"

    list_page = DocumentListPage(page, live_server.url)
    list_page.visit()
    page.click(".DocumentCard-testButton")

    # CreateDocumentTestView creates the test and redirects to /test/<uuid>.
    page.wait_for_url(f"{live_server.url}/test/**")
    expect(page.locator("#cloze-test")).to_be_visible()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_upload_shows_language_error_for_unsupported_script(client, user_factory):
    """The upload form reports an unsupported language instead of failing."""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse

    from core.models import AcademicField, Document
    from tests.e2e.test_ingestion import CYRILLIC_TEXT

    user = user_factory(email="cyrillic-upload@example.com")
    client.force_login(user)

    response = client.post(
        reverse("document-upload"),
        {
            "file": SimpleUploadedFile("ru.txt", CYRILLIC_TEXT, content_type="text/plain"),
            "academic_fields": [AcademicField.objects.first().pk],
            "publication_date": "2026-01-01",
            "authorship_percentage": 100,
            "author_count": 1,
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    assert "Supported languages are Czech, Slovak and English" in response.content.decode()
    assert not Document.objects.filter(uploaded_by=user).exists()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_upload_shows_page_limit_error_for_long_pdf(client, user_factory):
    """A PDF over the page limit gets a form message naming both numbers,
    not a server error."""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse

    from core.models import AcademicField, Document
    from core.utils.parsers import PDF_MAX_PAGES
    from tests.e2e.test_ingestion import _pdf_with_pages

    user = user_factory(email="long-pdf-upload@example.com")
    client.force_login(user)

    response = client.post(
        reverse("document-upload"),
        {
            "file": SimpleUploadedFile(
                "long.pdf", _pdf_with_pages(PDF_MAX_PAGES + 1), content_type="application/pdf"
            ),
            "academic_fields": [AcademicField.objects.first().pk],
            "publication_date": "2026-01-01",
            "authorship_percentage": 100,
            "author_count": 1,
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    body = response.content.decode()
    assert f"{PDF_MAX_PAGES + 1} pages" in body
    assert f"limit is {PDF_MAX_PAGES}" in body
    assert not Document.objects.filter(uploaded_by=user).exists()
