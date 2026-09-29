"""E2E coverage for entering correct answers on a try-mode test.

Walks an anonymous user through a demo document, fills every cloze blank
with the *correct* word read straight from the test's TestedParagraph row,
submits a success estimate, and asserts the Bayesian score page reports
overwhelming evidence of authorship. Exercises the full scoring path
end-to-end (template filter, POST handler, embedding scorer, authorship
posterior).
"""

import pytest
from playwright.sync_api import expect

from tests.e2e.pages import ClozeTestPage, TryDocumentSelectPage


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_try_mode_with_correct_answers_yields_perfect_score(live_server, page):
    from core.models import Test, TestedParagraph

    select = TryDocumentSelectPage(page, live_server.url)
    select.visit(language="cs")
    expect(page.locator(".document-list__item")).not_to_have_count(0)
    select.start_first_test()

    page.wait_for_selector("#cloze-test")
    expect(page.locator('#cloze-test input[name^="word_"]').first).to_be_visible()
    test_id = page.url.rstrip("/").rsplit("/", 1)[-1]
    test_obj = Test.objects.select_related("document").get(pk=test_id)

    cloze = ClozeTestPage(page, live_server.url)

    # Iterate paragraph-by-paragraph: read the currently-displayed paragraph
    # straight from the DB (the view marks it via answer_started_at), fill
    # the cloze inputs with its correct words, submit, repeat.
    for _ in range(200):
        # Read the next unanswered paragraph straight from the DB to learn
        # the correct word(s), then fill and submit.
        current = (
            TestedParagraph.objects.filter(
                test=test_obj,
                answer_started_at__isnull=False,
                answer_ended_at__isnull=True,
            )
            .select_related("word")
            .first()
        )
        if current is None:
            break

        correct_words = current.word.content.split(" ")
        try:
            cloze.fill_blanks_with_words(correct_words)
            cloze.submit()
        except Exception:
            break
        if not page.locator("#cloze-test").is_visible():
            break

    page.wait_for_selector("form.SuccessEstimateForm")
    cloze.submit_success_estimate()

    expect(page.locator(".test-summary__eyebrow")).to_have_text("Test result")

    # The authorship analysis is now the only view — chart + headline are
    # rendered immediately, no expand toggle.
    expect(page.locator(".aver-detail")).to_be_visible()

    # All answers correct -> posterior P(author) ≥ 95 % (exact value depends
    # on calibrated pA/pN, item count and the 10 % default prior) and the
    # verdict is "strong-author".
    headline_text = page.locator("#aver-headline-pct").inner_text()
    pct = float(headline_text.rstrip("%"))
    assert pct >= 95.0, f"Expected ≥95% P(author), got {headline_text!r}"
    expect(page.locator(".aver-verdict")).to_have_class(
        "aver-verdict aver-verdict--strong-author"
    )
    expect(page.locator("#aver-dist-chart")).to_be_visible()

    # Prior slider starts at the server's editorial default (10 %).
    slider = page.locator("#aver-prior-slider")
    expect(slider).to_have_value("10")
    expect(page.locator("#aver-prior-out")).to_have_text("10%")
    # Drag to 50 % — readout and posterior recompute live. Raising the prior
    # P(non-author) must lower P(author); the exact value depends on the
    # calibrated pA/pN and item count, so only assert the direction.
    slider.evaluate("el => { el.value = '50'; el.dispatchEvent(new Event('input')); }")
    expect(page.locator("#aver-prior-out")).to_have_text("50%")
    pct_after = float(page.locator("#aver-headline-pct").inner_text().rstrip("%"))
    assert pct_after < pct, f"Expected P(author) below {pct}% at 50% prior, got {pct_after!r}"
    # Slide back to the default — the headline returns to the server-rendered value.
    slider.evaluate("el => { el.value = '10'; el.dispatchEvent(new Event('input')); }")
    expect(page.locator("#aver-prior-out")).to_have_text("10%")
    expect(page.locator("#aver-headline-pct")).to_have_text(headline_text)
    expect(page.locator(".aver-verdict")).to_have_class(
        "aver-verdict aver-verdict--strong-author"
    )

    expect(page.locator("#aver-tg-scale button.is-active")).to_have_text("linear")
    expect(page.locator("#aver-tg-prior button.is-active")).to_have_text("yes")
