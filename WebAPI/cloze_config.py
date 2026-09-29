"""Shared cloze-class configuration.

Historically this lived inside ``worker.py``. The web tier now needs to know
the "target accepted count" too (so it can expose it in ``GET /jobs/<id>`` as
``progress.total_accepted``), and pulling ``worker.py`` into ``bottleAPI.py``
would drag every ML dependency into the web process. This module holds the
pure-Python parts only -- no numpy, no torch, no transformers.

Keep this file cheap to import.
"""

import json
import os
from typing import Dict, Optional

# Cloze-class extraction rules. See README section "Analysis configuration
# (cloze classes)" for the full field reference (pos_tags, num_of_words,
# match, allowed_pos_tags, LM_verify).
PICK_CLASSES = {
    "unigrams_NOUN": {"pos_tags": ["NOUN"], "num_of_words": 1, "LM_verify": True},
    "unigrams_ADJ":  {"pos_tags": ["ADJ"],  "num_of_words": 1, "LM_verify": True},
    # Must contain an adjective; every token must use one of these POS tags.
    # PRT is excluded so English particles like "to" / "up" (which look like
    # prepositions to end users) cannot slip into a trigram alongside an ADJ.
    "trigrams_w_ADJ": {
        "pos_tags": ["ADJ"],
        "allowed_pos_tags": ["VERB", "PRON", "NUM", "ADJ", "NOUN"],
        "num_of_words": 3,
        "match": "contains",
        "LM_verify": True,
    },
    "trigrams":         {"pos_tags": ["NOUN", "ADV", "ADJ"], "num_of_words": 3, "LM_verify": True},
    "bigrams_NOUN_ADJ": {"pos_tags": ["NOUN", "ADJ"],        "num_of_words": 2, "LM_verify": True},
    "bigrams_ADV_ADJ":  {"pos_tags": ["ADV", "ADJ"],         "num_of_words": 2, "LM_verify": True},
}

# Production default is 15. Evals set AVER_CLOZE_TOTAL=100 which scales the mix.
_DEFAULT_CLASSES_NUM: Dict[str, int] = {
    "unigrams_NOUN": 4,
    "unigrams_ADJ": 4,
    "trigrams_w_ADJ": 2,
    "trigrams": 2,
    "bigrams_NOUN_ADJ": 2,
    "bigrams_ADV_ADJ": 1,
}


def scale_classes_num(total: int, base: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """Keep the 4:4:2:2:2:1 mix. Round, then fix the sum to `total`.

    Production stays at 15. Eval sets AVER_CLOZE_TOTAL=100.
    """
    base = dict(base or _DEFAULT_CLASSES_NUM)
    if total <= 0:
        raise ValueError("cloze total must be positive")
    base_total = sum(base.values())
    if base_total <= 0:
        raise ValueError("base CLASSES_NUM sums to 0")
    scaled = {key: max(0, int(round(value * total / base_total)))
              for key, value in base.items()}
    delta = total - sum(scaled.values())
    order = sorted(scaled, key=lambda k: (-scaled[k], k))
    idx = 0
    while delta != 0 and order:
        key = order[idx % len(order)]
        if delta > 0:
            scaled[key] += 1
            delta -= 1
        elif scaled[key] > 0:
            scaled[key] -= 1
            delta += 1
        idx += 1
    return scaled


def load_classes_num(base: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """Production default is 15. Override with AVER_CLASSES_NUM or AVER_CLOZE_TOTAL."""
    base = dict(base or _DEFAULT_CLASSES_NUM)
    raw = os.getenv("AVER_CLASSES_NUM")
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("AVER_CLASSES_NUM must be a JSON object")
        parsed = {str(k): int(v) for k, v in parsed.items()}
        if set(parsed) != set(PICK_CLASSES):
            raise ValueError("AVER_CLASSES_NUM keys must match PICK_CLASSES")
        if any(value < 0 for value in parsed.values()) or sum(parsed.values()) <= 0:
            raise ValueError("AVER_CLASSES_NUM values must be nonnegative and sum above 0")
        return parsed
    total = os.getenv("AVER_CLOZE_TOTAL")
    if total is not None and str(total).strip() != "":
        return scale_classes_num(int(total), base)
    return base


def get_accepted_target() -> int:
    """Sum of ``CLASSES_NUM.values()`` -- how many candidate words the analyzer
    is aiming to return per document under the current env. Used by the web
    tier at enqueue time so ``GET /jobs/<id>`` can advertise
    ``progress.total_accepted`` without every client hard-coding the default.
    """
    return sum(load_classes_num().values())
