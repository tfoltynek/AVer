"""Bayesian authorship-probability computation over per-item correct/wrong.

Each test item has a known probability of a correct answer under two
hypotheses: H_A (author) and H_N (non-author). Given the binary outcome
vector x = (x_1, …, x_n), we compute:

  P(x | A) = ∏_i pA_i^{x_i} (1 - pA_i)^{1 - x_i}
  P(x | N) = ∏_i pN_i^{x_i} (1 - pN_i)^{1 - x_i}

and combine via Bayes' theorem with a prior π_N for non-authorship.

The chart displays the Poisson-binomial PMFs over the score statistic
S = Σ x_i for both hypotheses, with the user's observed score marked.

Skipped paragraphs (no answer recorded) count as incorrect — per Foltýnek
2026-05-25: "Přeskočený odstavec = špatná odpověď." Binarization rule:
`computed_grade == "correct"` → 1, anything else (incl. skipped) → 0.

Every graded blank enters the evidence pool. Words whose
`selection_method` has a SELECTOR_PROBS entry contribute that bucket's
(pA, pN); anything else (an unmapped selection category, a POS combination
that fits no calibrated subgroup, an unsupported language, a local pick
strategy) contributes FALLBACK_PROBS, the calibration values for randomly
removed words — per Foltýnek 2026-09-18, which replaced dropping those
blanks.
"""

from __future__ import annotations

import math

# pA/pN per calibration subgroup, all derived from Hitzinger's calibration
# study (B. Hitzinger, "Usability of cloze test for authorship verification",
# bachelor's thesis, FI MUNI 2025, https://is.muni.cz/th/nikqo/). The thesis
# has the method; its author reported the subgroup values by email on
# 2026-05-25, computed in his notebook pA_pN_computing.ipynb:
#   • ML unigrams: the notebook's 1 < ml_pos <= 40 subgroups, recounted
#     with the app's case-insensitive exact match, NOT the notebook's
#     similarity >= 0.7172 criterion. Nouns: 121/170 authors, 130/325
#     non-authors; adjectives: 28/41 authors, 16/71 non-authors.
#     scripts/audit_calibration.py reproduces the counts from the study's
#     CSV. The old values used similarity scoring and no rank filter.
#   • Trigrams: retain the notebook's similarity-based subgroup values.
#   • Bigrams: Hitzinger didn't include any bigram method in the email,
#     but the study's data covers them. We compute the overall bigram value
#     (n=2579) by the same formula; sub-strata by POS vary only modestly
#     so we don't split them further. The service does split its bigram
#     categories by POS; all of them map onto this one bucket.
#   • Randomly removed words: the service tops a document up with random
#     picks when its model finds too few, and marks them `category: random`.
#     They are a calibrated group like any other, with the same pair as
#     FALLBACK_PROBS below — the local analyzer's random strategy shares
#     both the name and the values.
SELECTOR_PROBS: dict[str, tuple[float, float]] = {
    "ml_noun_unigram":      (0.71176, 0.40000),
    "ml_adj_unigram":       (0.68293, 0.22535),
    "bigram":               (0.56110, 0.34434),
    "adj_adv_trigram":      (0.51163, 0.12840),
    "noun_adj_adv_trigram": (0.51667, 0.086486),
    "noun_adj_trigram":     (0.53468, 0.20450),
    "trigram_with_adj":     (0.52910, 0.20644),
    "random":               (0.70784, 0.43586),
}

# Blanks we cannot place in any group at all: a selection category the service
# reports but we do not map yet, a POS combination stanza cannot classify, an
# unsupported language, or a local pick strategy other than random. They are
# scored with the calibration values measured for randomly removed words,
# which is the most defensible thing to say about a word we know nothing
# about. Deliberately the same pair as SELECTOR_PROBS["random"].
FALLBACK_PROBS: tuple[float, float] = (0.70784, 0.43586)

# Where the results chart's prior slider starts. Stakeholder-set 10 %
# (2026-08, up from 5 %); it is a display default only, never persisted.
DEFAULT_PRIOR_NON_AUTHOR = 0.10

# 5-tier verdict thresholds, keyed off P(author). Per the AVer report
# (and confirmed by Foltýnek 2026-05-25, "Podle mě je lepší 5"):
#   ≥ 0.90 → strong-author
#   0.70–0.90 → probable-author
#   0.30–0.70 → inconclusive
#   0.10–0.30 → probable-non-author
#   < 0.10 → strong-non-author
VERDICT_TIERS: list[tuple[float, str]] = [
    (0.90, "strong-author"),
    (0.70, "probable-author"),
    (0.30, "inconclusive"),
    (0.10, "probable-non-author"),
    (0.00, "strong-non-author"),
]


def verdict_class(posterior_a: float) -> str:
    """Return the 5-tier verdict class for `posterior_a` (P(author))."""
    for threshold, label in VERDICT_TIERS:
        if posterior_a >= threshold:
            return label
    return "strong-non-author"

def selector_probabilities(method: str | None) -> tuple[float, float]:
    """Return (pA, pN) for a `selection_method`.

    A method in the calibration table gets its own pair; everything else,
    including None, gets FALLBACK_PROBS. Never returns None, so no caller
    has to decide what an uncalibrated blank is worth.
    """
    return SELECTOR_PROBS.get(method or "", FALLBACK_PROBS)


def poisson_binomial_pmf(probs: list[float]) -> list[float]:
    """Probability mass function of S = Σ X_i for independent Bernoulli X_i
    with success probabilities `probs`. Returns f where f[k] = P(S = k).

    Ports the O(n²) DP from the reference JS applet exactly.
    """
    n = len(probs)
    f = [0.0] * (n + 1)
    f[0] = 1.0
    for i, p in enumerate(probs):
        for k in range(i + 1, 0, -1):
            f[k] = f[k] * (1 - p) + f[k - 1] * p
        f[0] *= (1 - p)
    return f


def item_level_posterior(
    items: list[dict],
    prior_non_author: float,
) -> tuple[float, float]:
    """Return (P(A | x), P(N | x)) via item-level Naïve Bayes.

    `items` is a list of dicts with keys `pA`, `pN`, `correct` (bool).
    Uses log-likelihoods with max-subtract for numerical stability.
    """
    if not items:
        prior_a = 1 - prior_non_author
        return prior_a, prior_non_author

    prior_a = 1 - prior_non_author
    log_la = 0.0
    log_ln = 0.0
    for it in items:
        if it["correct"]:
            log_la += math.log(it["pA"])
            log_ln += math.log(it["pN"])
        else:
            log_la += math.log(1 - it["pA"])
            log_ln += math.log(1 - it["pN"])

    log_a = log_la + math.log(prior_a)
    log_n = log_ln + math.log(prior_non_author)
    max_log = max(log_a, log_n)
    num_a = math.exp(log_a - max_log)
    num_n = math.exp(log_n - max_log)
    denom = num_a + num_n
    return num_a / denom, num_n / denom


def report(
    paragraphs, *, prior_non_author: float = DEFAULT_PRIOR_NON_AUTHOR
) -> dict:
    """The one authorship result object every renderer reads.

    JSON-safe by construction: the template renders its fields, and the same
    dict is serialized for the chart script, which only *draws* — the
    posterior and verdict for every slider position (integer percents) are
    precomputed here so no math is ever re-implemented client-side. The 0 and
    100 endpoints are prior certainty (log(0) never enters the likelihood).
    """
    items = build_items(paragraphs)
    posterior_a, posterior_n = item_level_posterior(items, prior_non_author)

    posteriors_by_prior_pct: list[list[float]] = []
    verdicts_by_prior_pct: list[str] = []
    for pct in range(101):
        if pct == 0:
            pa, pn = 1.0, 0.0
        elif pct == 100:
            pa, pn = 0.0, 1.0
        else:
            pa, pn = item_level_posterior(items, pct / 100)
        posteriors_by_prior_pct.append([pa, pn])
        verdicts_by_prior_pct.append(verdict_class(pa))

    skipped = sum(1 for it in items if it.get("skipped"))
    return {
        "score": sum(1 for it in items if it["correct"]),
        "n": len(items),
        "skipped": skipped,
        "answered": len(items) - skipped,
        "posterior_author": posterior_a,
        "posterior_author_pct": posterior_a * 100,
        "posterior_non_author": posterior_n,
        "verdict": verdict_class(posterior_a),
        "prior_pct": int(round(prior_non_author * 100)),
        "pmf_author": poisson_binomial_pmf([it["pA"] for it in items]),
        "pmf_non_author": poisson_binomial_pmf([it["pN"] for it in items]),
        "posteriors_by_prior_pct": posteriors_by_prior_pct,
        "verdicts_by_prior_pct": verdicts_by_prior_pct,
    }


def build_items(paragraphs) -> list[dict]:
    """Turn a queryset/list of TestedParagraph into authorship items.

    Every paragraph becomes an item. A word with a calibrated
    `selection_method` carries its bucket's (pA, pN); a word without one
    (local-analyzer pick, MUNI pick whose POS combo fits no Hitzinger
    subgroup, NULL) carries FALLBACK_PROBS and is flagged `fallback`.
    Skipped paragraphs (no `answered_word`) binarize to incorrect.
    """
    items = []
    for p in paragraphs:
        method = getattr(p.word, "selection_method", None)
        pA, pN = selector_probabilities(method)
        is_answered = bool((p.answered_word or "").strip())
        items.append({
            "pA": pA,
            "pN": pN,
            "correct": is_answered and p.computed_grade == "correct",
            "selector": method,
            "fallback": method not in SELECTOR_PROBS,
            "skipped": not is_answered,
        })
    return items
