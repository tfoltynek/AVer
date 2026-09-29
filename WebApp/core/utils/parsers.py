import pymupdf
import regex as re

from core.utils.extract_paragraphs import (
    contains_bibliographic_abbreviations,
    has_valid_paragraph_format,
    is_acknowledgment,
    is_bibliography_heading,
    is_caption,
    is_declaration,
    is_majority_single_line,
    is_numeric_line,
    is_toc,
    is_valid_paragraph,
    remove_excess_whitespaces,
    repair_page_split_paragraphs,
)

PDF_MAX_PAGES = 200


class PdfTooLong(ValueError):
    """The PDF has more pages than PDF_MAX_PAGES; carries both numbers."""

    def __init__(self, pages: int, limit: int = PDF_MAX_PAGES):
        super().__init__(f"PDF has {pages} pages; the limit is {limit}.")
        self.pages = pages
        self.limit = limit


def parse_pdf(file_path) -> list[str]:
    doc = pymupdf.open(file_path)
    if doc.page_count > PDF_MAX_PAGES:
        raise PdfTooLong(doc.page_count)
    # Phase 1 - Just getting those textboxes out and split them into paragraphs
    paragraphs = []
    for page in doc:
        current_paragraph = ""
        # Assuming 'page' is your TextPage object
        blocks = page.get_text("blocks", sort=False)

        for block in blocks:
            line = block[4]

            if line.strip().isdigit() or is_toc(line):
                continue

            # Potencial start of paragraph
            if re.match(r"^\b\p{Lu}\p{Ll}*\b", line):
                current_paragraph += "\n\n"

            # Fix split words at the end of line
            fixed_line = re.sub(r"\s(\p{L}+)-\n", r"\n\1", line)

            current_paragraph += fixed_line.strip()
            if bool(re.search(r".*[^\P{L}\d_][\.\?\!]$", current_paragraph)):
                current_paragraph += "\n\n"
            if fixed_line == line:
                current_paragraph += " "  # Concatenate lines to form a paragraph
            paragraphs.append(current_paragraph)  # Add the paragraph to the list
            current_paragraph = ""  # Reset for the next paragraph

    # Phase 2 - Getting plausible paragraphs
    fixed_paragraphs = []
    found_bibliography = False
    paragraphs_to_process = (
        "\n".join(paragraphs).split("\n\n")
        if is_majority_single_line(paragraphs)
        else paragraphs
    )

    for index, paragraph in enumerate(paragraphs_to_process):
        if found_bibliography:
            break

        split_paragraphs = "\n".join(
            repair_page_split_paragraphs(
                [para for para in paragraph.split("\n") if len(para.strip())]
            )
        ).split("\n\n")

        for part in split_paragraphs:
            if found_bibliography:
                break

            lines_in_block = repair_page_split_paragraphs(
                [para for para in part.split("\n") if len(para.strip())]
            )

            for line in lines_in_block:
                if (
                    index > len(paragraphs_to_process) / 2
                    and len(lines_in_block) == 1
                    and is_bibliography_heading(line.strip())
                ):
                    found_bibliography = True
                    break

                if is_caption(line) or is_toc(line) or is_numeric_line(line.strip()):
                    continue

                current_paragraph += line.strip()
                current_paragraph += " "

            if (
                has_valid_paragraph_format(current_paragraph)
                and is_valid_paragraph(current_paragraph)
                and not is_acknowledgment(current_paragraph)
                and not is_declaration(current_paragraph)
                and not contains_bibliographic_abbreviations(current_paragraph)
            ):
                fixed_paragraphs.append(remove_excess_whitespaces(current_paragraph))
            current_paragraph = ""

    extracted_text = "\n\n".join(fixed_paragraphs)
    extracted_text = re.sub(r"([\p{Ll},])\n\n(\p{Ll})", r"\1 \2", extracted_text)
    extracted_text = re.sub(
        r"^\p{Ll}[^\.\?!]*[\.?!]\s*", r"", extracted_text, flags=re.MULTILINE
    )

    return extracted_text.split("\n\n")
