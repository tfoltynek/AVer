"""E2E coverage for auth-form error states.

The happy paths live in test_auth.py — this file exercises the failure
branches so a regression in form validation or error-rendering shows up
in the suite instead of silently swallowing user mistakes.
"""

import pytest
from playwright.sync_api import expect

from tests.e2e.pages import LogInPage, SignUpPage


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_login_with_wrong_password_shows_error(
    live_server, page, user_factory
):
    user = user_factory(email="wrongpw@example.com")

    login_page = LogInPage(page, live_server.url)
    login_page.visit()
    login_page.fill_and_submit(email=user.email, password="not-the-real-pw")

    # Stays on /log-in (HX-Redirect only fires on success) and renders an
    # errorlist somewhere in the form. The exact wording is i18n, so match
    # the structural marker rather than the string.
    expect(page).to_have_url(f"{live_server.url}/log-in")
    expect(page.locator("form .errorlist").first).to_be_visible()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_with_mismatched_passwords_shows_error(live_server, page):
    signup = SignUpPage(page, live_server.url)
    signup.visit()

    page.select_option('select[name="education"]', "bachelors")
    page.select_option('select[name="gender"]', "M")
    page.fill('input[name="email"]', "mismatch@example.com")
    page.fill('input[name="password"]', "first-password-123")
    page.fill('input[name="password_confirm"]', "different-password-456")
    page.locator('input[name="academic_fields"]').first.check()
    page.click('button[type="submit"]:has-text("Sign up")')

    # Form rejects; we should still be on /sign-up with an errorlist.
    expect(page).to_have_url(f"{live_server.url}/sign-up")
    expect(page.locator("form .errorlist").first).to_be_visible()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_without_academic_field_shows_error(live_server, page):
    """`AcademicFieldsRequiredMixin` enforces ≥1 field; UI should surface it."""
    signup = SignUpPage(page, live_server.url)
    signup.visit()

    page.select_option('select[name="education"]', "bachelors")
    page.select_option('select[name="gender"]', "M")
    page.fill('input[name="email"]', "no-field@example.com")
    page.fill('input[name="password"]', "Sup3rStrong!pass")
    page.fill('input[name="password_confirm"]', "Sup3rStrong!pass")
    # Intentionally do NOT check any academic_fields checkbox.
    page.click('button[type="submit"]:has-text("Sign up")')

    expect(page).to_have_url(f"{live_server.url}/sign-up")
    expect(page.locator("form .errorlist").first).to_be_visible()
