"""Unit tests for the Bayesian authorship module. No DB, no browser."""

from __future__ import annotations

import math

import pytest

from core.authorship import (
    DEFAULT_PRIOR_NON_AUTHOR,
    FALLBACK_PROBS,
    SELECTOR_PROBS,
    build_items,
    item_level_posterior,
    poisson_binomial_pmf,
    selector_probabilities,
    verdict_class,
)


class _FakeWord:
    def __init__(self, selection_method: str | None = None):
        self.selection_method = selection_method


class _FakeParagraph:
    def __init__(
        self,
        answered: str | None,
        grade: str,
        selection_method: str | None = None,
    ):
        self.word = _FakeWord(selection_method)
        self.answered_word = answered
        self.computed_grade = grade


def test_pmf_uniform_half_matches_binomial():
    # All p=0.5 ⇒ pmf is Binomial(n, 0.5) = C(n,k)/2^n
    pmf = poisson_binomial_pmf([0.5] * 4)
    expected = [1 / 16, 4 / 16, 6 / 16, 4 / 16, 1 / 16]
    for a, b in zip(pmf, expected):
        assert math.isclose(a, b, abs_tol=1e-12)


def test_pmf_all_certain():
    pmf = poisson_binomial_pmf([1.0, 1.0, 1.0])
    assert pmf == pytest.approx([0.0, 0.0, 0.0, 1.0], abs=1e-12)


def test_pmf_all_zero():
    pmf = poisson_binomial_pmf([0.0, 0.0, 0.0])
    assert pmf == pytest.approx([1.0, 0.0, 0.0, 0.0], abs=1e-12)


def test_pmf_sums_to_one():
    probs = [0.84, 0.27, 0.70, 0.40, 0.55, 0.61, 0.13, 0.92]
    pmf = poisson_binomial_pmf(probs)
    assert math.isclose(sum(pmf), 1.0, abs_tol=1e-12)
    assert len(pmf) == len(probs) + 1


def test_posterior_empty_returns_prior():
    pA, pN = item_level_posterior([], 0.05)
    assert math.isclose(pA, 0.95)
    assert math.isclose(pN, 0.05)


def test_posterior_symmetric_at_half_prior():
    # When pA == pN for every item, the likelihoods cancel and the
    # posterior reduces to the prior.
    items = [
        {"pA": 0.5, "pN": 0.5, "correct": True},
        {"pA": 0.7, "pN": 0.7, "correct": False},
    ]
    pA, pN = item_level_posterior(items, 0.5)
    assert math.isclose(pA, 0.5, abs_tol=1e-12)
    assert math.isclose(pN, 0.5, abs_tol=1e-12)


def test_posterior_matches_reference_applet():
    # Identical setup to the JS reference: 15 items, mixed M1 & M2,
    # 5% prior non-author. Author should win convincingly. Uses the
    # applet's literal pA/pN pairs (0.84, 0.27) and (0.70, 0.40), not
    # the current SELECTOR_PROBS — this locks the math implementation,
    # not the project's calibrated probabilities.
    items = [
        {"pA": 0.84, "pN": 0.27, "correct": True},
        {"pA": 0.84, "pN": 0.27, "correct": False},
        {"pA": 0.84, "pN": 0.27, "correct": True},
        {"pA": 0.84, "pN": 0.27, "correct": True},
        {"pA": 0.84, "pN": 0.27, "correct": False},
        {"pA": 0.70, "pN": 0.40, "correct": True},
        {"pA": 0.70, "pN": 0.40, "correct": False},
        {"pA": 0.70, "pN": 0.40, "correct": True},
        {"pA": 0.70, "pN": 0.40, "correct": True},
        {"pA": 0.70, "pN": 0.40, "correct": False},
        {"pA": 0.84, "pN": 0.27, "correct": True},
        {"pA": 0.84, "pN": 0.27, "correct": True},
        {"pA": 0.70, "pN": 0.40, "correct": False},
        {"pA": 0.70, "pN": 0.40, "correct": True},
        {"pA": 0.84, "pN": 0.27, "correct": True},
    ]
    pA, pN = item_level_posterior(items, 0.05)
    assert math.isclose(pA + pN, 1.0, abs_tol=1e-12)
    # Locked to the JS reference applet's output for this exact input,
    # to 12 decimals — same DP, identical floats.
    assert math.isclose(pA, 0.9989704452312771, abs_tol=1e-12)


def test_posterior_extreme_evidence_against_author():
    # Many wrong answers should flip the verdict despite a moderate prior.
    items = [{"pA": 0.9, "pN": 0.1, "correct": False} for _ in range(10)]
    pA, pN = item_level_posterior(items, 0.5)
    assert pN > 0.999


def test_build_items_counts_skipped_as_incorrect():
    # Per Foltýnek 2026-05-25: "Přeskočený odstavec = špatná odpověď."
    # Skipped items stay in the evidence pool but binarize to 0.
    paragraphs = [
        _FakeParagraph("answer", "correct", selection_method="bigram"),
        _FakeParagraph(None, "", selection_method="bigram"),           # skipped
        _FakeParagraph("  ", "", selection_method="trigram_with_adj"),  # blank
        _FakeParagraph("answer", "incorrect", selection_method="trigram_with_adj"),
    ]
    items = build_items(paragraphs)
    assert len(items) == 4
    assert [it["correct"] for it in items] == [True, False, False, False]
    assert [it.get("skipped", False) for it in items] == [False, True, True, False]


def test_build_items_scores_uncalibrated_selectors_with_the_fallback():
    # Per Foltýnek 2026-09-18, a blank that fits no calibrated bucket is no
    # longer dropped: it is scored with the pA/pN of randomly removed words.
    # That covers local pick strategies other than random, which are valid
    # selection_method values under the source/shape taxonomy, and words
    # whose selection category we do not map (NULL).
    paragraphs = [
        _FakeParagraph("x", "correct", selection_method="most_used_content_word"),
        _FakeParagraph("x", "correct"),                                # method NULL
        _FakeParagraph("x", "correct", selection_method="bigram"),     # calibrated
    ]
    items = build_items(paragraphs)
    assert len(items) == 3
    assert [it["fallback"] for it in items] == [True, True, False]
    for it in items[:2]:
        assert (it["pA"], it["pN"]) == FALLBACK_PROBS
    assert (items[2]["pA"], items[2]["pN"]) == SELECTOR_PROBS["bigram"]


def test_random_is_a_calibrated_group_not_a_fallback():
    # The service marks its top-up picks `category: random` and the local
    # analyzer has a random strategy; both are a measured group, so they must
    # not be reported as uncalibrated even though the pair is the same.
    items = build_items([_FakeParagraph("x", "correct", selection_method="random")])
    assert items[0]["fallback"] is False
    assert (items[0]["pA"], items[0]["pN"]) == SELECTOR_PROBS["random"]
    assert SELECTOR_PROBS["random"] == FALLBACK_PROBS


def test_build_items_binarization_only_correct():
    paragraphs = [
        _FakeParagraph("x", "correct", selection_method="bigram"),
        _FakeParagraph("x", "incorrect", selection_method="bigram"),
    ]
    items = build_items(paragraphs)
    assert [it["correct"] for it in items] == [True, False]


def test_build_items_uses_calibrated_probs():
    # Each scored item must carry the exact pA/pN from SELECTOR_PROBS.
    for method in [
        "ml_noun_unigram",
        "ml_adj_unigram",
        "bigram",
        "adj_adv_trigram",
        "noun_adj_adv_trigram",
        "noun_adj_trigram",
        "trigram_with_adj",
    ]:
        p = _FakeParagraph("x", "correct", selection_method=method)
        items = build_items([p])
        assert len(items) == 1, f"{method} should have been scored"
        pA, pN = selector_probabilities(method)
        assert items[0]["pA"] == pA
        assert items[0]["pN"] == pN
        assert items[0]["selector"] == method
        assert items[0]["fallback"] is False


def test_selector_probabilities_unknown_returns_the_fallback():
    # No method outside the table has its own calibration, so all of them —
    # including NULL — share the randomly-removed-word pair.
    for method in [
        "not_a_real_method",
        "ml",
        "trigram",
        "unigrams_NOUN",  # a raw service category, never a stored method
        "most_used_content_word",
        None,
    ]:
        assert selector_probabilities(method) == FALLBACK_PROBS


def test_calibration_constants_are_pinned():
    # R3: the calibration lives in code, so the values are pinned here. A new
    # study means editing both the table and this test, deliberately.
    assert SELECTOR_PROBS == {
        "ml_noun_unigram": (0.71176, 0.40000),
        "ml_adj_unigram": (0.68293, 0.22535),
        "bigram": (0.56110, 0.34434),
        "adj_adv_trigram": (0.51163, 0.12840),
        "noun_adj_adv_trigram": (0.51667, 0.086486),
        "noun_adj_trigram": (0.53468, 0.20450),
        "trigram_with_adj": (0.52910, 0.20644),
        "random": (0.70784, 0.43586),
    }
    assert FALLBACK_PROBS == (0.70784, 0.43586)


def test_default_prior_is_10_percent():
    assert DEFAULT_PRIOR_NON_AUTHOR == 0.10


def test_verdict_class_five_tiers():
    # Boundaries are inclusive on the lower bound.
    assert verdict_class(0.95) == "strong-author"
    assert verdict_class(0.90) == "strong-author"
    assert verdict_class(0.89) == "probable-author"
    assert verdict_class(0.70) == "probable-author"
    assert verdict_class(0.69) == "inconclusive"
    assert verdict_class(0.50) == "inconclusive"
    assert verdict_class(0.30) == "inconclusive"
    assert verdict_class(0.29) == "probable-non-author"
    assert verdict_class(0.10) == "probable-non-author"
    assert verdict_class(0.09) == "strong-non-author"
    assert verdict_class(0.00) == "strong-non-author"


# ── report ──────────────────────────────────────────────────────────────────


def _calibrated_paragraphs():
    return [
        _FakeParagraph("aphorism", "correct", "ml_noun_unigram"),
        _FakeParagraph("guess", "incorrect", "bigram"),
        _FakeParagraph(None, "", "noun_adj_trigram"),  # skipped → incorrect
    ]


def test_report_matches_the_component_functions():
    from core.authorship import report

    paragraphs = _calibrated_paragraphs()
    r = report(paragraphs)

    items = build_items(paragraphs)
    pa, pn = item_level_posterior(items, DEFAULT_PRIOR_NON_AUTHOR)
    assert r["n"] == 3
    assert r["score"] == 1
    assert r["skipped"] == 1
    assert r["posterior_author"] == pytest.approx(pa)
    assert r["posterior_author_pct"] == pytest.approx(pa * 100)
    assert r["posterior_non_author"] == pytest.approx(pn)
    assert r["verdict"] == verdict_class(pa)
    assert r["prior_pct"] == 10
    assert r["pmf_author"] == pytest.approx(
        poisson_binomial_pmf([i["pA"] for i in items])
    )
    assert sum(r["pmf_author"]) == pytest.approx(1.0)
    assert sum(r["pmf_non_author"]) == pytest.approx(1.0)


def test_report_precomputes_every_slider_prior():
    from core.authorship import report

    paragraphs = _calibrated_paragraphs()
    r = report(paragraphs)
    items = build_items(paragraphs)

    assert len(r["posteriors_by_prior_pct"]) == 101
    assert len(r["verdicts_by_prior_pct"]) == 101
    # The slider's whole range must agree with the server-side math — this is
    # the agreement the old client-side reimplementation never verified.
    for pct in (1, 5, 27, 50):
        pa, pn = item_level_posterior(items, pct / 100)
        assert r["posteriors_by_prior_pct"][pct][0] == pytest.approx(pa)
        assert r["posteriors_by_prior_pct"][pct][1] == pytest.approx(pn)
        assert r["verdicts_by_prior_pct"][pct] == verdict_class(pa)
    # Endpoints are certainty, not a log(0) crash.
    assert r["posteriors_by_prior_pct"][0] == [1.0, 0.0]
    assert r["posteriors_by_prior_pct"][100] == [0.0, 1.0]


def test_report_empty_pool_passes_prior_through():
    from core.authorship import report

    r = report([])  # a test with no items at all

    assert r["n"] == 0
    assert r["posterior_author"] == pytest.approx(1 - DEFAULT_PRIOR_NON_AUTHOR)


def test_report_counts_fallback_items_in_n():
    # n is every graded blank now, not just the calibrated ones.
    from core.authorship import report

    r = report([
        _FakeParagraph("x", "correct", "bigram"),
        _FakeParagraph("x", "correct", "random"),
        _FakeParagraph("x", "incorrect"),
    ])

    assert r["n"] == 3
    assert r["score"] == 2


def test_report_answered_excludes_skipped():
    # The results summary reads `answered`; `n` is every item, skips included.
    from core.authorship import report

    r = report([
        _FakeParagraph("x", "correct", "bigram"),
        _FakeParagraph(None, "", "bigram"),
        _FakeParagraph("x", "incorrect", "random"),
    ])

    assert (r["n"], r["answered"], r["skipped"]) == (3, 2, 1)


def test_five_fallback_items_match_the_user_guide_table():
    # The user guide prints these six rows in section 5.7, "Jak výsledek
    # číst" (docs/user-guide/sections/05-vysledek.typ), to show how little a
    # five-item test moves the default prior. They are FALLBACK_PROBS at
    # DEFAULT_PRIOR_NON_AUTHOR — the calibration every figure in that chapter
    # uses — so editing the constants must break here, not in print.
    from core.authorship import report

    rows = []
    for k in range(6):
        paragraphs = [
            _FakeParagraph("x", "correct" if i < k else "incorrect")
            for i in range(5)
        ]
        assert all(it["fallback"] for it in build_items(paragraphs))
        r = report(paragraphs)
        rows.append((round(r["posterior_author_pct"]), r["verdict"]))

    assert rows == [
        (25, "probable-non-author"),
        (51, "inconclusive"),
        (77, "probable-author"),
        (91, "strong-author"),
        (97, "strong-author"),
        (99, "strong-author"),
    ]
