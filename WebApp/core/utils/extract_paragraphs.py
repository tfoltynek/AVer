import regex as re

from core.utils.nlp import get_spacy_nlp

CITATION_PATTERN = re.compile(r"\.(\s*\([^()]*\)|\s*\[[^[]*\])?$")

CAPTION_PATTERN = re.compile(
    "|".join([
        r"\bObrázek\b", r"\bObr\b", r"\bTabulka\b", r"\bGraf\b",
        r"\bObrázok\b", r"\bTabuľka\b",
        r"\bFigure\b", r"\bFig\b", r"\bTable\b", r"\bGraph\b",
        r"\bKód\b", r"\bCode\b", r"\bUkázka kódu\b",
        r"\bZdroj\b", r"\bSource\b",
    ]),
    re.IGNORECASE,
)

ACKNOWLEDGMENT_PATTERN = re.compile(
    "|".join([
        r"\bDěkuji\b", r"\bDěkujeme\b", r"\bPoděkov", r"\bPoděkování\b",
        r"\bĎakujem\b", r"\bĎakujeme\b", r"\bPoďakovanie\b",
        r"\bThanks\b", r"\bGratitude\b", r"\bThank you\b",
        r"\bTo thank\b", r"\bAcknowledgment\b", r"\bAcknowledgement\b",
    ]),
    re.IGNORECASE,
)

DECLARATION_PATTERN = re.compile(
    "|".join([
        r"\bProhlášení\b", r"\bProhlašuji\b", r"\bProhlašujeme\b", r"\bPotvrzuji\b",
        r"\bPrehlásenie\b", r"\bPrehlasujem\b", r"\bPrehlasujeme\b", r"\bPotvrdzujem\b",
        r"\bDeclaration\b", r"\bDeclare\b", r"\bWe declare\b", r"\bI declare\b",
    ]),
    re.IGNORECASE,
)

BIBLIOGRAPHIC_ABBREV_PATTERN = re.compile(
    "|".join([r"\bISBN\b", r"\bDOI\b", r"\bISSN\b", r"\bURN\b", r"\barXiv\b"]),
    re.IGNORECASE,
)

BIBLIOGRAPHY_HEADING_PATTERN = re.compile(
    "|".join([
        r"^(\S*\s)?\bReferences\b$",
        r"^(\S*\s)?\bBibliography\b$",
        r"^(\S*\s)?\bWorks cited\b$",
        r"^(\S*\s)?\bLiterature cited\b$",
        r"^(\S*\s)?\bReference list\b$",
        r"^(\S*\s)?\bZdroje\b$",
        r"^(\S*\s)?\bLiteratura\b$",
        r"^(\S*\s)?\bSeznam literatury\b$",
        r"^(\S*\s)?\bReferencie\b$",
        r"^(\S*\s)?\bZoznam použitých zdrojov\b$",
    ]),
    re.IGNORECASE,
)

TOC_PATTERN = re.compile(r"(?s).*(\.\s*){3,}.*")
STARTS_WITH_DIGIT_OR_BRACKET_PATTERN = re.compile(r"^[0-9\(\)]")
STARTS_WITH_UPPERCASE_WORD_PATTERN = re.compile(r"^\p{L}+\b")
STARTS_WITH_NON_ALPHANUMERIC_PATTERN = re.compile(r"[^\p{L}\p{N}].*[^.]$")
NUMERIC_LINE_PATTERN = re.compile(r"^\s*\d+(\.\d+)?\s*%?\s*$")
EXCESS_WHITESPACE_PATTERN = re.compile(r"\s+")


def is_valid_paragraph(paragraph):
    doc = get_spacy_nlp()(paragraph)

    sentence_count = len(
        [sentence for sentence in list(doc.sents) if len(sentence) > 4]
    )
    word_count = len([token.text for token in doc if token.is_alpha])

    min_sentence_count = 2
    min_word_count = 15

    if (sentence_count >= min_sentence_count and word_count >= min_word_count) or (
        word_count >= min_word_count
        and bool(CITATION_PATTERN.search(paragraph.strip()))
    ):
        return True
    else:
        return False


def repair_page_split_paragraphs(lines):
    if not len(lines):
        return lines
    average_length = sum(len(line) for line in lines) / len(lines)
    threshold = 0.8 * average_length
    fixed_lines = [f"{line}\n" if len(line) < threshold else line for line in lines]
    return fixed_lines


def is_caption(text):
    return bool(CAPTION_PATTERN.search(text))


def is_acknowledgment(text):
    return bool(ACKNOWLEDGMENT_PATTERN.search(text))


def is_declaration(text):
    return bool(DECLARATION_PATTERN.search(text))


def is_toc(text):
    return bool(TOC_PATTERN.search(text))


def has_valid_paragraph_format(s):
    starts_with_digit_or_bracket = bool(STARTS_WITH_DIGIT_OR_BRACKET_PATTERN.match(s))
    starts_with_uppercase_word = bool(STARTS_WITH_UPPERCASE_WORD_PATTERN.match(s))
    starts_with_non_alphanumeric_and_not_ends_with_period = bool(
        STARTS_WITH_NON_ALPHANUMERIC_PATTERN.match(s)
    )

    return (
        starts_with_uppercase_word
        and not starts_with_digit_or_bracket
        and not starts_with_non_alphanumeric_and_not_ends_with_period
    )


def contains_bibliographic_abbreviations(text):
    return bool(BIBLIOGRAPHIC_ABBREV_PATTERN.search(text))


def is_bibliography_heading(text):
    return bool(BIBLIOGRAPHY_HEADING_PATTERN.search(text))


def is_majority_single_line(strings):
    single_line_count = 0
    for s in strings:
        if "\n" not in s:
            single_line_count += 1
    return single_line_count > len(strings) / 2


def is_numeric_line(line):
    lines = line.split("\n")
    for single_line in lines:
        if not NUMERIC_LINE_PATTERN.match(single_line.strip()):
            return False
    return True


def remove_excess_whitespaces(text):
    return EXCESS_WHITESPACE_PATTERN.sub(" ", text).strip()
