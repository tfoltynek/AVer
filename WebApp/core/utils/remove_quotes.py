import re

from core.utils.nlp import get_spacy_nlp


def find_quotes(text):
    quotes = ["‘", "’", "‚", "‛", "“", "”", "„", "‟", '"', " '", "' ", "`", "''"]
    positions = []
    for position, word in enumerate(text):
        for quote in quotes:
            if re.search(quote, word):
                if position not in positions:
                    positions.append(position)
                else:
                    positions.remove(position)
    # TODO: Find smarter way to overcome document's bad quotes
    if len(positions) % 2:
        return [0, len(text)]

    return positions


def find_tokenized_quotes(text):
    doc = get_spacy_nlp()(text)
    # TODO: Add finding quotes based on langugage specific quote signs
    quotes = ["‘", "’", "‚", "‛", "“", "”", "„", "‟", '"', " '", "' ", "`", "''"]
    positions = []
    for position, word in enumerate([token.text for token in doc]):
        for quote in quotes:
            if re.search(quote, word):
                if position not in positions:
                    positions.append(position)
                else:
                    positions.remove(position)
    # TODO: Find smarter way to overcome document's bad quotes
    if len(positions) % 2:
        return [0, len(text)]

    return positions


def find_tokenized_quotes_ranges(text) -> list[tuple[int, int]]:
    quotes_positions = find_tokenized_quotes(text)
    ranges_count = len(quotes_positions)
    ranges: list[tuple[int, int]] = []
    if ranges_count >= 2:
        i = 0
        while i < ranges_count:
            ranges.insert(0, (quotes_positions[i], quotes_positions[i + 1]))
            i += 2

    return ranges


def find_quotes_ranges(text) -> list[tuple[int, int]]:
    quotes_positions = find_quotes(text)
    ranges_count = len(quotes_positions)
    ranges: list[tuple[int, int]] = []
    if ranges_count >= 2:
        i = 0
        while i < ranges_count:
            ranges.insert(0, (quotes_positions[i], quotes_positions[i + 1]))
            i += 2

    return ranges


def remove_quoted_regions(text) -> str:
    ranges_to_remove = find_quotes_ranges(text)

    croppted_text = text
    for remove_range in ranges_to_remove:
        start = remove_range[0]
        end = remove_range[1] + 1
        croppted_text = croppted_text[:start] + croppted_text[end:]

    return croppted_text
