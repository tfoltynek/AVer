"""E2E coverage for the public try-mode flow.

Anonymous user picks a demo document, takes the cloze test, submits a success
estimate, and lands on the result page.
"""

import pytest
from playwright.sync_api import expect

from tests.e2e.pages import ClozeTestPage, TryDocumentSelectPage


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_anonymous_try_flow(live_server, page):
    select = TryDocumentSelectPage(page, live_server.url)
    select.visit(language="cs")

    expect(page.locator(".document-list__item")).not_to_have_count(0)
    select.start_first_test()

    test_page = ClozeTestPage(page, live_server.url)
    page.wait_for_selector("#cloze-test")

    # Answer paragraphs until we exit the cloze form. Cap iterations as a
    # safety net — typical demo docs have a handful of paragraphs.
    for _ in range(60):
        try:
            test_page.fill_blanks_with("answer")
            test_page.submit()
        except Exception:
            break
        if not page.locator("#cloze-test").is_visible():
            break

    page.wait_for_selector('form.SuccessEstimateForm')
    test_page.submit_success_estimate()

    # After redirect, score page renders with the test-result eyebrow and
    # the document title as the page h1.
    expect(page.locator(".test-summary__eyebrow")).to_have_text("Test result")
    expect(page.locator("h1.test-summary__title")).to_be_visible()
