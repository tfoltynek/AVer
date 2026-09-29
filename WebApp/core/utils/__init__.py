from pathlib import Path

import docx
import regex as re
from django.core.files.uploadedfile import UploadedFile

from core.utils.detect_language import detect_language
from core.utils.extract_paragraphs import is_valid_paragraph
from core.utils.parsers import parse_pdf


def fix_hyphenated_words(text):
    # Regex pattern to match a word split by a hyphen at the end of a line
    # The pattern captures the part before the hyphen, the hyphen, and the continuation after it.
    pattern = r"(\w+)-\s+(\w+)"

    # Substitute the split word (with hyphen and space) with the merged word
    fixed_text = re.sub(pattern, r"\1\2", text)

    return fixed_text


def remove_footnote_numbers(text):
    # Regex pattern to match footnote numbers directly after or before words
    # \b - Word boundary to ensure the number is attached to the word
    # \d+ - Matches one or more digits
    pattern = r"(\b\w+)\d+|\d+(\w+\b)"

    # Replace footnote numbers attached to words
    fixed_text = re.sub(pattern, r"\1\2", text)

    return fixed_text


def remove_excess_whitespaces(text):
    return re.sub(r"\s+", " ", text).strip()


def filter_valid_paragraphs(paragraphs):
    filtered_paragraphs = [
        remove_excess_whitespaces(paragraph)
        for paragraph in paragraphs
        if is_valid_paragraph(paragraph)
    ]

    return filtered_paragraphs


def get_paragraph_language(paragraph):
    return detect_language(paragraph)


# Language ID needs only a modest amount of text; ~20k chars already gives
# Lingua full confidence on cs/sk/en. The cap keeps detection both deterministic
# and bounded in cost: the old `random.sample(...)` was unseeded, so the same
# upload came out cs most of the time and sk a few percent of the time; a full
# join would instead be safe but slow (~2.9M chars/s, so a 30 MB upload is ~10s
# of detection, synchronously, in the request). Leading paragraphs in order are
# representative because a document's language is ~constant, and a short
# front-matter abstract is a negligible fraction of the budget.
DOC_LANGUAGE_SAMPLE_CHARS = 20_000


def get_document_language(paragraphs):
    sample: list[str] = []
    total = 0
    for paragraph in paragraphs:
        sample.append(paragraph)
        total += len(paragraph) + 1
        if total >= DOC_LANGUAGE_SAMPLE_CHARS:
            break
    return detect_language(" ".join(sample))


def extract_text_from_document(file):
    file_format = Path(file.name).suffix.lower()
    match file_format:
        case ".pdf":
            return extract_text_from_pdf(file)
        case ".docx":
            return extract_text_from_docx(file)
        case ".txt":
            return extract_text_from_txt(file)
        # case ".md":
        #     return extract_text_from_md(file)
        case _:
            raise ValueError("Unsupported file type")


def extract_text_from_pdf(file: UploadedFile):
    paragraphs = parse_pdf(file.temporary_file_path())
    # parsed = parser.from_file(file.temporary_file_path())

    # content = parsed["content"]
    # paragraphs = [
    #     remove_excess_whitespaces(
    #         remove_footnote_numbers(fix_hyphenated_words(paragraph))
    #     )
    #     for paragraph in content.split("\n\n")
    # ]
    return paragraphs


def extract_text_from_docx(file):
    doc = docx.Document(file.temporary_file_path())
    paragraphs = [paragraph.text for paragraph in doc.paragraphs]
    return paragraphs


def extract_text_from_txt(file):
    # Process larger files stored temporarily
    with open(file.temporary_file_path()) as f:
        file_content = f.read()  # Read the file content into a string
        paragraphs = re.split(r"\r\n\r\n|\n\n|\r\r", file_content)

    return paragraphs


# def extract_text_from_md(file):
#     md_content = file.read().decode("utf-8")
#     return markdown.markdown(md_content)
