import functools

import spacy


@functools.cache
def get_spacy_nlp():
    return spacy.load("xx_sent_ud_sm")
