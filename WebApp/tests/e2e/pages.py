"""Lightweight page objects for the e2e suite.

Selectors live here so test files stay readable and template changes only
touch one place.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect


class SignUpPage:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def visit(self) -> None:
        self.page.goto(f"{self.base_url}/sign-up")

    def fill_and_submit(
        self,
        *,
        email: str,
        password: str,
        education: str = "bachelors",
        gender: str = "M",
    ) -> None:
        self.page.select_option('select[name="education"]', education)
        self.page.select_option('select[name="gender"]', gender)
        self.page.fill('input[name="email"]', email)
        self.page.fill('input[name="password"]', password)
        self.page.fill('input[name="password_confirm"]', password)
        self.page.locator('input[name="academic_fields"]').first.check()
        self.page.click('button[type="submit"]:has-text("Sign up")')


class LogInPage:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def visit(self) -> None:
        self.page.goto(f"{self.base_url}/log-in")

    def fill_and_submit(self, *, email: str, password: str) -> None:
        self.page.fill('input[name="username"]', email)
        self.page.fill('input[name="password"]', password)
        self.page.click('button[type="submit"]:has-text("Log in")')


class DocumentUploadPage:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def visit(self) -> None:
        self.page.goto(f"{self.base_url}/documents/upload")

    def fill_and_submit(self, *, file_path: Path, publication_date: str = "2024-01-15") -> None:
        self.page.set_input_files('input[name="file"]', str(file_path))
        self.page.fill('input[name="author_count"]', "1")
        self.page.fill('input[name="publication_date"]', publication_date)
        self.page.locator('input[name="academic_fields"]').first.check()
        self.page.click('#submit-button')


class DocumentListPage:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def visit(self) -> None:
        self.page.goto(f"{self.base_url}/documents")

    def expect_document_with_title(self, title: str) -> None:
        expect(
            self.page.locator(".DocumentCard-title", has_text=title)
        ).to_be_visible()

    def expect_empty(self) -> None:
        expect(self.page.locator(".DocumentCardList-emptyMessage")).to_be_visible()


class SettingsPage:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def visit(self) -> None:
        self.page.goto(f"{self.base_url}/settings")

    def click_edit_academic_fields(self) -> None:
        self.page.click('#academic-fields-infobox button:has-text("Edit")')

    def submit_academic_fields(self) -> None:
        self.page.click('#update-academic-fields-form button[type="submit"]')

    def check_academic_field_by_index(self, index: int) -> None:
        checkbox = self.page.locator(
            '#update-academic-fields-form input[name="academic_fields"]'
        ).nth(index)
        checkbox.check()


class TryDocumentSelectPage:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def visit(self, language: str = "cs") -> None:
        self.page.goto(f"{self.base_url}/try/documents/{language}")

    def start_first_test(self) -> None:
        self.page.locator(".document-list__item a.Button").first.click()


class ClozeTestPage:
    """Cloze-test page: form has dynamic word_N inputs injected by `blank_to_input`."""

    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url

    def fill_blanks_with(self, value: str = "answer") -> int:
        inputs = self.page.locator('#cloze-test input[name^="word_"]')
        count = inputs.count()
        for i in range(count):
            inputs.nth(i).fill(value)
        return count

    def fill_blanks_with_words(self, words: list[str]) -> int:
        """Fill each word_N input with the corresponding word from `words`.

        If the form has more inputs than `words` provides, leftover inputs
        get the last word repeated (rare; happens only with mismatched data).
        Returns the number of inputs filled.
        """
        inputs = self.page.locator('#cloze-test input[name^="word_"]')
        count = inputs.count()
        for i in range(count):
            inputs.nth(i).fill(words[i] if i < len(words) else words[-1])
        return count

    def _wait_for_htmx(self) -> None:
        """Block until htmx has executed in the page.

        The Skip button is wired only through hx-post (type="button"), so it
        is a dead control until htmx.min.js — a classic script at the end of
        <body> — has run. #cloze-test appears in the DOM before that script,
        so a fast click would otherwise fire into a page that isn't
        listening yet and its POST would never happen.
        """
        self.page.wait_for_function("() => window.htmx !== undefined")

    def submit(self) -> None:
        # Wait for the POST response so the next iteration of any caller-side
        # loop reads consistent server state (the previous paragraph's
        # answer_ended_at and the next one's answer_started_at both committed).
        self._wait_for_htmx()
        with self.page.expect_response(
            lambda r: "/test/" in r.url and r.request.method == "POST"
        ):
            self.page.click('#cloze-test button.Test-nextButton')
        # Allow htmx to finish swapping/redirecting before returning.
        self.page.wait_for_load_state("networkidle")

    def skip(self) -> None:
        self._wait_for_htmx()
        with self.page.expect_response(
            lambda r: "/test/" in r.url and r.request.method == "POST"
        ):
            self.page.click('#cloze-test button.Test-skipButton')
        self.page.wait_for_load_state("networkidle")

    def submit_success_estimate(self) -> None:
        """Submit the success-estimate slider and wait for the score page nav.

        The form is a plain HTML POST to /test/<id>/success-estimate which
        302s back to /test/<id>; both URLs need to settle before assertions.
        """
        button = self.page.locator("form.SuccessEstimateForm button[type='submit']")
        button.wait_for(state="visible")
        button.scroll_into_view_if_needed()
        with self.page.expect_navigation(wait_until="load"):
            button.click()
