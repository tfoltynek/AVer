import random
import re
from collections import Counter

from core.utils.get_language_stopwords import get_language_stopwords
from core.utils.nlp import get_spacy_nlp
from core.utils.remove_quotes import (
    remove_quoted_regions,
)

from ..models import Paragraph, Word


def process_text(paragraph, word):
    # Split text into paragraphs (assuming paragraphs are separated by double newlines)
    sentences = re.split(r"(?<=[.!?]) +", paragraph)

    # Check each sentence for the specified word
    for i, sentence in enumerate(sentences):
        # Look for the specified word using word boundaries to avoid partial matches
        if re.search(rf"\b{re.escape(word)}\b", sentence):
            # Replace the word with "<<BLANK>>"
            modified_sentence = re.sub(rf"\b{re.escape(word)}\b", "<<BLANK>>", sentence)

            # Return the index of the paragraph and the modified sentence
            return i, modified_sentence

    # If no match is found, return None
    return None, None


class DocumentAnalyzer:
    """
    A class used for document analyzing with spaCy's multilingual 'xx_sent_ud_sm' model.

    Attributes
    ----------
    paragraphs : list[Paragraph]
        list of Paragraphs used for analysis
    document_language_code : str
        document language code used for deciding what stopwords to use during analysis

    Methods
    -------
    get_analyzed_words()
        Returns interesting analyzed words
    """

    def __init__(self, *, paragraphs: list[Paragraph], document_language):
        self.all_paragraphs = [
            p for p in paragraphs if p.language_id == document_language.pk
        ]
        self.language_stopwords = get_language_stopwords(document_language.code)

        self.nlp = get_spacy_nlp()

        self.any_word_counter = Counter()
        self.no_stopword_counter = Counter()

        self.interesting_words = []

        self._scan_document()
        self._analyze_document()

    def _is_valid_word(self, word: str, no_stopword: bool = False) -> bool:
        """Check if the word is valid (alphabetic and non-stopword if specified)."""
        return word.isalpha() and (
            word.lower() not in self.language_stopwords if no_stopword else True
        )

    def _in_quote(self, position, quoted_ranges):
        """Check if a word is within a quoted region."""
        return any(start <= position < end for start, end in quoted_ranges)

    def _scan_document(self):
        """Scans the document, counts valid words with and without stopwords."""
        for paragraph in self.all_paragraphs:
            paragraph_without_quotes = remove_quoted_regions(paragraph.content)
            doc = self.nlp(paragraph_without_quotes)

            tokens = [token.text for token in doc if token.is_alpha]

            # Count valid words
            for token in tokens:
                if self._is_valid_word(token):
                    self.any_word_counter[token.lower()] += 1
                if self._is_valid_word(token, no_stopword=True):
                    self.no_stopword_counter[token.lower()] += 1

    def _analyze_document(self):
        """Analyzes the document and identifies words based on various criteria."""
        most_common_content_words = [
            word for word in self.no_stopword_counter.most_common(5) if word[1] > 2
        ]

        for paragraph in self.all_paragraphs:
            # quoted_ranges = find_tokenized_quotes_ranges(paragraph.content)
            doc = self.nlp(paragraph.content)
            tokens = [token.text for token in doc]

            self._find_content_words(
                most_common_content_words,
                paragraph,
                tokens,
                [],
                "most_used_content_word",
            )
            # self._find_content_words(
            #     least_common_content_words,
            #     paragraph,
            #     tokens,
            #     quoted_ranges,
            #     "least_used_content_word",
            # )
            self._find_random_words(paragraph, tokens, [])

    def _word_metadata(self, paragraph, position, word, selection_method):
        """Build the persistable dict for one local-analyzer pick."""
        sentence_index, sentence_blanked = process_text(paragraph.content, word)
        return {
            "paragraph_id": paragraph.pk,
            "content": word,
            "index": position,
            "source": Word.Source.LOCAL_ANALYZER,
            "shape": Word.shape_for(word),
            "selection_method": selection_method,
            "sentence_blanked": sentence_blanked,
            "sentence_index": sentence_index,
        }

    def _find_content_words(
        self, content_words, paragraph, tokens, quoted_ranges, selection_method
    ):
        """Finds and stores content words (most/least used) in the interesting words list."""
        for position, word in enumerate(tokens):
            if not self._in_quote(position, quoted_ranges) and word in [
                w[0] for w in content_words
            ]:
                self.interesting_words.append(
                    self._word_metadata(paragraph, position, word, selection_method)
                )

    def _find_random_words(self, paragraph, tokens, quoted_ranges):
        """Randomly selects a few words and adds them to the interesting words list."""
        random_words = [
            (i, word)
            for i, word in enumerate(tokens)
            if not self._in_quote(i, quoted_ranges) and self._is_valid_word(word)
        ]
        for position, word in random.sample(random_words, min(3, len(random_words))):
            self.interesting_words.append(
                self._word_metadata(paragraph, position, word, "random")
            )

    def get_analyzed_words(self):
        """Returns the list of analyzed words."""
        return self.interesting_words
