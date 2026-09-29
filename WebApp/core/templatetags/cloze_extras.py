import re

from django import template
from django.utils.translation import gettext_lazy as _

from core.models import TestedParagraph, Word

register = template.Library()


def shorten_text_around_inputs(
    text: str,
    keyword,
    context_size: int = 3,
) -> str:
    if not text:
        return text

    sentence_pattern = r"(?<=[.!?])\s+"
    sentences = re.split(sentence_pattern, text)
    blank_indices = [i for i, sentence in enumerate(sentences) if keyword in sentence]

    if not blank_indices:
        return text

    inner_context = (blank_indices[0], blank_indices[-1])

    selected_sentences = []
    start_index = max(inner_context[0] - context_size, 0)
    end_index = min(inner_context[1] + context_size + 1, len(sentences))
    for i in range(start_index, end_index):
        selected_sentences.append(sentences[i])

    return " ".join(selected_sentences)


@register.filter
def blank_to_input(word: Word):
    blank_count = word.sentence_blanked.count("<<BLANK>>")
    full_sentence: str = word.sentence_blanked
    words = word.content.split(" ")

    # If there is only one blank and more words, replace the blank with all words (for compatibility with new input format)
    if blank_count == 1 and len(words) > 1:
        full_sentence = full_sentence.replace("<<BLANK>>", " ".join(words), 1)
    else:
        for w in words:
            full_sentence = full_sentence.replace("<<BLANK>>", w, 1)

    full_paragraph = word.paragraph.content
    full_paragraph = full_paragraph.replace(full_sentence, "<<INSERT>>")
    input_sentence = word.sentence_blanked
    # If there is only one blank and more words, replace the blank with all input fields (for compatibility with new input format)
    if blank_count == 1 and len(words) > 1:
        inputs = []
        for idx, __ in enumerate(words):
            inputs.append(
                f'<input id="word_{idx + 1}" name="word_{idx + 1}" type="text" {"autofocus" if idx == 0 else ""} size=8 spellcheck="false" placeholder="{_("Type here")}" required/></input>'
            )
        input_sentence = input_sentence.replace("<<BLANK>>", " ".join(inputs), 1)
    else:
        for index in range(blank_count):
            input_sentence = input_sentence.replace(
                "<<BLANK>>",
                f'<input id="word_{index + 1}" name="word_{index + 1}" type="text" {"autofocus" if index == 0 else ""} size=8 spellcheck="false" placeholder="{_("Type here")}" required/></input>',
                1,
            )

    final_paragraph = full_paragraph.replace("<<INSERT>>", input_sentence)
    final_paragraph = shorten_text_around_inputs(final_paragraph, "</input>")
    return final_paragraph


@register.filter
def blank_to_preview(word: Word):
    blank_count = word.sentence_blanked.count("<<BLANK>>")
    full_sentence: str = word.sentence_blanked
    words = word.content.split(" ")

    # If there is only one blank and more words, replace the blank with all words (for compatibility with new input format)
    if blank_count == 1 and len(words) > 1:
        full_sentence = full_sentence.replace("<<BLANK>>", " ".join(words), 1)
    else:
        for w in words:
            full_sentence = full_sentence.replace("<<BLANK>>", w, 1)

    full_paragraph = word.paragraph.content
    full_paragraph = full_paragraph.replace(full_sentence, "<<INSERT>>")
    input_sentence = word.sentence_blanked
    # Special case: one blank, more words (new input format)
    if blank_count == 1 and len(words) > 1:
        previews = []
        for idx, w in enumerate(words):
            previews.append(w + "><><")
        input_sentence = input_sentence.replace("<<BLANK>>", " ".join(previews), 1)
    else:
        for index in range(blank_count):
            input_sentence = input_sentence.replace(
                "<<BLANK>>",
                words[index] + "><><",
                1,
            )

    final_paragraph = full_paragraph.replace("<<INSERT>>", input_sentence)
    final_paragraph = shorten_text_around_inputs(final_paragraph, "><><")
    final_paragraph = final_paragraph.replace("><><", "")
    return final_paragraph


@register.filter
def blank_to_answer(paragraph: TestedParagraph):
    full_sentence: str = paragraph.word.sentence_blanked
    test_words = paragraph.word.content.split(" ")
    answered_words = (
        paragraph.answered_word.split(" ")
        if paragraph.answered_word is not None
        else [None for __ in test_words]
    )
    blank_count = full_sentence.count("<<BLANK>>")

    # For multi-word expecteds (bigram/trigram), the whole phrase is scored as
    # one cosine-similarity unit. Color every reveal-span with the paragraph's
    # overall grade so the UI matches the score logic. For unigrams this is the
    # same as the per-word grade.
    paragraph_grade = "" if answered_words[0] is None else (paragraph.computed_grade or "")

    # Special case: one blank, more words (new input format)
    if blank_count == 1 and len(test_words) > 1:
        full_sentence = full_sentence.replace("<<BLANK>>", " ".join(test_words), 1)
    else:
        for w in test_words:
            full_sentence = full_sentence.replace("<<BLANK>>", w, 1)

    full_paragraph = paragraph.word.paragraph.content
    full_paragraph = full_paragraph.replace(full_sentence, "<<INSERT>>")
    input_sentence = paragraph.word.sentence_blanked

    def make_answer_span(idx, word):
        return f'<span class="answer-result answer-result__{paragraph_grade}">{word}</span>'

    if blank_count == 1 and len(test_words) > 1:
        answers = [make_answer_span(idx, word) for idx, word in enumerate(test_words)]
        input_sentence = input_sentence.replace("<<BLANK>>", " ".join(answers), 1)
    else:
        for idx, word in enumerate(test_words):
            input_sentence = input_sentence.replace(
                "<<BLANK>>",
                make_answer_span(idx, word),
                1,
            )

    final_paragraph = full_paragraph.replace("<<INSERT>>", input_sentence)
    final_paragraph = shorten_text_around_inputs(final_paragraph, "</span>")
    return final_paragraph
