"""Ingestion: from an uploaded file to an analyzed-pending Document.

`ingest_upload` owns extraction, filtering, language detection, persistence
and the analysis kickoff, so all of it is testable by handing it a file
object — no HTTP POST, no browser.
"""

import datetime
from pathlib import Path

import pytest
from django.core.files.uploadedfile import TemporaryUploadedFile

from core.models import AcademicField, AnalysisJob, Document, Paragraph

SAMPLE_FILE = Path(__file__).parent / "fixtures" / "sample.txt"

# Enough prose to pass the paragraph filter, in a script none of cs / sk / en
# use, so lingua cannot assign a supported language.
CYRILLIC_PARAGRAPH = (
    "Это тестовый документ, написанный на русском языке. Он содержит несколько "
    "предложений и абзацев, чтобы проверить, как ведёт себя детектор языка."
)
CYRILLIC_TEXT = "\n\n".join([CYRILLIC_PARAGRAPH] * 6).encode("utf-8")


def _uploaded(name: str, content: bytes) -> TemporaryUploadedFile:
    """A real TemporaryUploadedFile, matching TemporaryFileUploadHandler
    (the only configured handler), so temporary_file_path() works."""
    upload = TemporaryUploadedFile(name, "text/plain", len(content), None)
    upload.write(content)
    upload.file.flush()
    upload.seek(0)
    return upload


def _metadata():
    return {
        "publication_date": datetime.date(2026, 1, 1),
        "author_count": 1,
        "authorship_percentage": 100,
        "academic_fields": list(AcademicField.objects.all()[:1]),
    }


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_ingest_persists_document_and_requests_analysis(user_factory):
    from core.ingestion import ingest_upload

    user = user_factory(email="ingest@example.com")
    upload = _uploaded("sample.txt", SAMPLE_FILE.read_bytes())
    metadata = _metadata()

    document = ingest_upload(upload, metadata=metadata, user=user)

    assert document.title == "sample"
    assert document.uploaded_by == user
    assert document.paragraphs.count() >= 5
    assert document.language_id == "en"
    assert document.analysis_state == "pending", (
        "a successful ingest must have requested exactly one AI analysis"
    )
    assert list(document.academic_fields.all()) == metadata["academic_fields"]
    assert document.publication_date == metadata["publication_date"]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_ingest_rejects_documents_with_too_little_text(user_factory):
    from core.ingestion import UnusableDocument, ingest_upload

    user = user_factory(email="ingest-short@example.com")
    upload = _uploaded("short.txt", b"Too short.\n\nNot enough text here.\n")

    with pytest.raises(UnusableDocument):
        ingest_upload(upload, metadata=_metadata(), user=user)

    assert not Document.objects.filter(uploaded_by=user).exists(), (
        "a rejected upload must persist nothing"
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_ingest_rejects_documents_with_undetectable_language(user_factory):
    """Enough text, but in a script none of cs / sk / en use: reject, do not 500."""
    from core.ingestion import UnsupportedLanguage, UnusableDocument, ingest_upload

    user = user_factory(email="ingest-cyrillic@example.com")
    upload = _uploaded("ru.txt", CYRILLIC_TEXT)

    with pytest.raises(UnsupportedLanguage):
        ingest_upload(upload, metadata=_metadata(), user=user)

    assert issubclass(UnsupportedLanguage, UnusableDocument), (
        "callers that only know UnusableDocument must keep catching it"
    )
    assert not Document.objects.filter(uploaded_by=user).exists()
    assert not AnalysisJob.objects.filter(document__uploaded_by=user).exists()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_ingest_falls_back_to_document_language_for_undetectable_paragraphs(
    user_factory, monkeypatch
):
    """A paragraph lingua cannot place inherits the document language."""
    from core.ingestion import ingest_upload

    monkeypatch.setattr("core.ingestion.get_paragraph_language", lambda _p: None)
    user = user_factory(email="ingest-fallback@example.com")

    document = ingest_upload(
        _uploaded("sample.txt", SAMPLE_FILE.read_bytes()),
        metadata=_metadata(),
        user=user,
    )

    languages = set(
        Paragraph.objects.filter(document=document).values_list("language_id", flat=True)
    )
    assert languages == {document.language_id} == {"en"}


def _pdf_with_pages(count: int) -> bytes:
    import pymupdf

    pdf = pymupdf.open()
    for _ in range(count):
        pdf.new_page()
    return pdf.tobytes()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_ingest_rejects_pdf_over_the_page_limit(user_factory):
    """A PDF longer than PDF_MAX_PAGES is refused like any other unusable
    document, with the page count on the error, and nothing is persisted."""
    from core.ingestion import OversizedDocument, UnusableDocument, ingest_upload
    from core.utils.parsers import PDF_MAX_PAGES

    user = user_factory(email="ingest-long-pdf@example.com")
    upload = _uploaded("long.pdf", _pdf_with_pages(PDF_MAX_PAGES + 1))

    with pytest.raises(OversizedDocument) as excinfo:
        ingest_upload(upload, metadata=_metadata(), user=user)

    assert excinfo.value.pages == PDF_MAX_PAGES + 1
    assert excinfo.value.limit == PDF_MAX_PAGES
    assert issubclass(OversizedDocument, UnusableDocument), (
        "callers that only know UnusableDocument must keep catching it"
    )
    assert not Document.objects.filter(uploaded_by=user).exists()
