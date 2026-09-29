"""ORM-level coverage for the `loaddocuments` command.

The command seeds the whole e2e session (see tests/e2e/conftest.py), so its
failure modes are expensive: an unmatched word used to raise IndexError and
abort the entire corpus load. It also matched paragraphs across every demo
document, so a fragment shared by two texts could attach a word to the wrong
one. Both are pinned here.
"""

import json

import pytest
from django.core.management import call_command

from core.models import Document, Paragraph, Word


def _write_pair(directory, name, language, paragraphs, words):
    (directory / f"{name}_request.json").write_text(
        json.dumps(
            {
                "language": language,
                "document_id": f"doc-{name}",
                "paragraphs": [
                    {"id": f"p-{name}-{i}", "content": text}
                    for i, text in enumerate(paragraphs)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (directory / f"{name}_response.json").write_text(
        json.dumps({"document_id": f"doc-{name}", "words": words}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_word_is_matched_inside_its_own_document(tmp_path):
    """Two documents share a sentence; each word must land in its own."""
    shared = "The quick brown fox jumps over the lazy dog."
    _write_pair(
        tmp_path,
        "aaa_first",
        "en",
        [shared],
        [
            {
                "paragraph_id": "p-aaa_first-0",
                "index": 3,
                "content": "fox",
                "sentence_blanked": "The quick brown <<BLANK>> jumps over the lazy dog.",
                "sentence_index": 0,
                "predictions": None,
                "additional_info": "",
            }
        ],
    )
    _write_pair(
        tmp_path,
        "bbb_second",
        "en",
        [shared],
        [
            {
                "paragraph_id": "p-bbb_second-0",
                "index": 7,
                "content": "dog",
                "sentence_blanked": "The quick brown fox jumps over the lazy <<BLANK>>.",
                "sentence_index": 0,
                "predictions": None,
                "additional_info": "",
            }
        ],
    )

    call_command("loaddocuments", str(tmp_path))

    first = Document.objects.get(file="aaa_first_request.json")
    second = Document.objects.get(file="bbb_second_request.json")
    fox = Word.objects.get(content="fox")
    dog = Word.objects.get(content="dog")
    assert fox.paragraph.document_id == first.id
    assert dog.paragraph.document_id == second.id


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_unmatched_word_is_skipped_not_fatal(tmp_path):
    """A word whose sentence is absent must not abort the rest of the load."""
    _write_pair(
        tmp_path,
        "ccc_partial",
        "en",
        ["A paragraph that really exists in the document."],
        [
            {
                "paragraph_id": "p-ccc_partial-0",
                "index": 1,
                "content": "nowhere",
                "sentence_blanked": "A sentence from a completely different text <<BLANK>> indeed.",
                "sentence_index": 0,
                "predictions": None,
                "additional_info": "",
            },
            {
                "paragraph_id": "p-ccc_partial-0",
                "index": 2,
                "content": "really",
                "sentence_blanked": "A paragraph that <<BLANK>> exists in the document.",
                "sentence_index": 0,
                "predictions": None,
                "additional_info": "",
            },
        ],
    )

    call_command("loaddocuments", str(tmp_path))

    document = Document.objects.get(file="ccc_partial_request.json")
    words = Word.objects.filter(paragraph__document=document)
    assert [w.content for w in words] == ["really"]
    assert Paragraph.objects.filter(document=document).count() == 1
