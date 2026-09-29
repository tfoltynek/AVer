# Real document fixtures

Drop a `.pdf` (or `.docx` / `.txt`) here and run:

    uv run pytest tests/e2e/test_documents.py::test_upload_real_document --browser chromium --headed

The test picks up the first matching file in this directory, uploads it
through the regular UI flow, and asserts it appears in the document list.
If the directory is empty the test is skipped.

This folder is gitignored except for this README so your test documents
do not get committed.
