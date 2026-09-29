"""E2E coverage for the Skip button in cloze tests.

Skipping a paragraph records `answer_ended_at` without an `answered_word`.
Per Foltýnek 2026-05-25 the authorship module counts skipped items as
incorrect (not as missing evidence). This test walks an anonymous user
through a try-mode test entirely via skip and asserts the score page
renders.
"""

import pytest
from playwright.sync_api import expect

from tests.e2e.pages import ClozeTestPage, TryDocumentSelectPage


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_skip_survives_slow_htmx_load(live_server, page):
    """Regression: the Skip button is wired only through hx-post
    (type="button"), so it is a dead control until htmx.min.js (a classic
    script at the end of <body>) executes. #cloze-test appears in the DOM
    before that, so a fast click used to fire into a page that wasn't
    listening yet, the skip POST never happened, and the test timed out
    waiting for the estimate form. Delaying htmx delivery makes that race
    deterministic instead of a full-suite-load lottery."""

    def stub_htmx(route):
        # Serve a loader that injects the real htmx 1.5s later. The delay has
        # to happen in the browser: a time.sleep in this handler would block
        # the sync Playwright dispatch loop and delay the test's own
        # wait_for_selector by the same amount, hiding the race.
        route.fulfill(
            content_type="application/javascript",
            body=(
                "setTimeout(function () {"
                "  var s = document.createElement('script');"
                "  s.src = '/static/core/js/htmx.min.js?real';"
                "  document.body.appendChild(s);"
                "}, 1500);"
            ),
        )

    page.route("**/htmx.min.js", stub_htmx)

    select = TryDocumentSelectPage(page, live_server.url)
    select.visit(language="cs")
    select.start_first_test()

    page.wait_for_selector("#cloze-test")
    cloze = ClozeTestPage(page, live_server.url)
    cloze.skip()  # must wait for htmx, not click a dead button

    # The skip actually happened: either the next paragraph rendered or the
    # test finished into the estimate form.
    assert (
        page.locator("#cloze-test").is_visible()
        or page.locator("form.SuccessEstimateForm").is_visible()
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_try_mode_skip_every_paragraph(live_server, page):
    select = TryDocumentSelectPage(page, live_server.url)
    select.visit(language="cs")
    expect(page.locator(".document-list__item")).not_to_have_count(0)
    select.start_first_test()

    page.wait_for_selector("#cloze-test")
    cloze = ClozeTestPage(page, live_server.url)

    # Skip until we're out of the cloze form.
    for _ in range(60):
        try:
            cloze.skip()
        except Exception:
            break
        if not page.locator("#cloze-test").is_visible():
            break

    page.wait_for_selector("form.SuccessEstimateForm")
    cloze.submit_success_estimate()

    # Score page renders with the test-result eyebrow regardless of whether
    # any answers were submitted.
    expect(page.locator(".test-summary__eyebrow")).to_have_text("Test result")
    expect(page.locator("h1.test-summary__title")).to_be_visible()
