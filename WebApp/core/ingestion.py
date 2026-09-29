"""Document ingestion: from an uploaded file to an analyzed-pending Document.

One door for "what happens to an uploaded file": extraction, paragraph
filtering, language detection, persistence and the analysis kickoff all
live behind `ingest_upload`, so the rules are exercisable without an HTTP
POST and the upload view shrinks to form handling.
"""

from pathlib import Path

from core.models import Document, Paragraph
from core.tasks import request_analysis
from core.utils import (
    extract_text_from_document,
    filter_valid_paragraphs,
    get_document_language,
    get_paragraph_language,
)
from core.utils.parsers import PdfTooLong

MIN_PLAUSIBLE_PARAGRAPHS = 5


class UnusableDocument(Exception):
    """The uploaded file has too little usable text to build tests from."""


class UnsupportedLanguage(UnusableDocument):
    """No supported language (cs / sk / en) could be detected for the document.

    In practice a document written in another script: it passes the paragraph
    filter, but lingua (built for cs / sk / en only) assigns it no language.
    """


class OversizedDocument(UnusableDocument):
    """The PDF has more pages than the parser accepts (``pages`` > ``limit``)."""

    def __init__(self, pages: int, limit: int):
        super().__init__(f"PDF has {pages} pages; the limit is {limit}")
        self.pages = pages
        self.limit = limit


def ingest_upload(uploaded_file, *, metadata: dict, user) -> Document:
    """Extract, filter, detect language, persist, and kick off the analysis.

    ``metadata`` is DocumentUploadForm.cleaned_data (publication_date,
    author_count, authorship_percentage, academic_fields). Raises
    ``UnusableDocument`` when fewer than ``MIN_PLAUSIBLE_PARAGRAPHS``
    paragraphs survive filtering, ``UnsupportedLanguage`` (a subclass)
    when no supported language can be detected and ``OversizedDocument``
    (a subclass) for a PDF over the page limit; all before anything is
    persisted.
    """
    try:
        paragraphs = extract_text_from_document(uploaded_file)
    except PdfTooLong as exc:
        raise OversizedDocument(exc.pages, exc.limit) from exc
    plausible_paragraphs = filter_valid_paragraphs(paragraphs)

    if len(plausible_paragraphs) < MIN_PLAUSIBLE_PARAGRAPHS:
        raise UnusableDocument(
            f"only {len(plausible_paragraphs)} plausible paragraphs"
        )

    language = get_document_language(plausible_paragraphs)
    if language is None:
        raise UnsupportedLanguage("no supported language detected")

    document = Document.objects.create(
        file=uploaded_file,
        title=Path(uploaded_file.name).stem,
        language_id=language,
        publication_date=metadata["publication_date"],
        author_count=metadata["author_count"],
        authorship_percentage=metadata["authorship_percentage"],
        uploaded_by=user,
    )
    document.academic_fields.set(metadata["academic_fields"])

    # A paragraph lingua cannot place (a quotation in another script, say)
    # inherits the document language rather than being dropped: the MUNI
    # request carries the document language anyway, and the per-paragraph
    # value only steers POS tagging of the words that come back.
    Paragraph.objects.bulk_create(
        [
            Paragraph(
                document=document,
                content=paragraph,
                language_id=get_paragraph_language(paragraph) or language,
            )
            for paragraph in plausible_paragraphs
        ]
    )

    request_analysis(document, "ai")
    return document
