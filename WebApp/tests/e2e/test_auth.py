"""E2E coverage for the auth flow: signup, login, logout."""

import pytest
from playwright.sync_api import expect

from tests.e2e.pages import LogInPage, SignUpPage


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_redirects_to_user_languages(live_server, page):
    """New signups get bounced to /user-languages by CheckNativeLanguageProficiencyMiddleware."""
    signup = SignUpPage(page, live_server.url)
    signup.visit()
    signup.fill_and_submit(
        email="newuser@example.com",
        password="Sup3rStrong!pass",
    )
    page.wait_for_url(f"{live_server.url}/user-languages")
    expect(page).to_have_url(f"{live_server.url}/user-languages")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_login_logout_login(live_server, page, user_factory, user_password):
    """Existing user with a native language can log in, log out, log in again."""
    user = user_factory(email="returning@example.com")

    login_page = LogInPage(page, live_server.url)
    login_page.visit()
    login_page.fill_and_submit(email=user.email, password=user_password)
    page.wait_for_url(f"{live_server.url}/dashboard")

    page.goto(f"{live_server.url}/log-out")
    page.wait_for_url(f"{live_server.url}/log-in")

    login_page.fill_and_submit(email=user.email, password=user_password)
    page.wait_for_url(f"{live_server.url}/dashboard")
    expect(page).to_have_url(f"{live_server.url}/dashboard")
