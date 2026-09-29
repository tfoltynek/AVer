"""E2E coverage for the change-password flow.

The settings page now exposes a "Change" button on the password infobox
that opens a modal dialog. POSTing the form updates the user's password
without logging them out (`update_session_auth_hash`).
"""

import re

import pytest
from playwright.sync_api import expect


def _open_password_dialog(page, base_url):
    page.goto(f"{base_url}/settings")
    page.click("#password-infobox button:has-text('Change')")
    page.wait_for_selector("#password-change-form")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_password_change_happy_path(
    live_server, page, user_factory, login, user_password
):
    """Open the dialog, enter old + new pw twice, confirm DB and session."""
    user = user_factory(email="pw-change@example.com")
    login(user)
    _open_password_dialog(page, live_server.url)

    new_password = "Br4nd-New-Pa55!"
    page.fill('#password-change-form input[name="old_password"]', user_password)
    page.fill('#password-change-form input[name="new_password"]', new_password)
    page.fill(
        '#password-change-form input[name="new_password_confirm"]', new_password
    )
    page.click("#password-change-form button[type='submit']")

    # Dialog closes; the OOB-swapped infobox surfaces the "just updated" badge.
    expect(page.locator("#password-infobox .InfoBox-success")).to_be_visible()
    expect(page.locator("#dialog")).to_have_count(0)

    # Password actually rotated in the DB.
    user.refresh_from_db()
    assert user.check_password(new_password)
    assert not user.check_password(user_password)

    # Session survived: navigating away does not redirect to /log-in.
    page.goto(f"{live_server.url}/dashboard")
    expect(page).to_have_url(f"{live_server.url}/dashboard")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_password_change_wrong_current_password(
    live_server, page, user_factory, login, user_password
):
    user = user_factory(email="pw-wrong-old@example.com")
    login(user)
    _open_password_dialog(page, live_server.url)

    page.fill('#password-change-form input[name="old_password"]', "not-the-real-pw")
    page.fill('#password-change-form input[name="new_password"]', "Br4nd-New-Pa55!")
    page.fill(
        '#password-change-form input[name="new_password_confirm"]', "Br4nd-New-Pa55!"
    )
    page.click("#password-change-form button[type='submit']")

    # Form stays open with an errorlist; dialog should still be visible.
    expect(page.locator("#password-change-form .errorlist").first).to_be_visible()
    expect(page.locator("#dialog")).to_be_visible()

    user.refresh_from_db()
    assert user.check_password(user_password), "password must not change on bad old-pw"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_password_change_mismatched_new_passwords(
    live_server, page, user_factory, login, user_password
):
    user = user_factory(email="pw-mismatch@example.com")
    login(user)
    _open_password_dialog(page, live_server.url)

    page.fill('#password-change-form input[name="old_password"]', user_password)
    page.fill('#password-change-form input[name="new_password"]', "Sup3rStrong!one")
    page.fill(
        '#password-change-form input[name="new_password_confirm"]', "Sup3rStrong!two"
    )
    page.click("#password-change-form button[type='submit']")

    expect(page.locator("#password-change-form .errorlist").first).to_be_visible()
    expect(page.locator("#dialog")).to_be_visible()

    user.refresh_from_db()
    assert user.check_password(user_password)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_password_change_rejects_weak_password(
    live_server, page, user_factory, login, user_password
):
    """Django's password validators must reject obviously weak choices."""
    user = user_factory(email="pw-weak@example.com")
    login(user)
    _open_password_dialog(page, live_server.url)

    page.fill('#password-change-form input[name="old_password"]', user_password)
    # "12345" trips the NumericPasswordValidator (and MinimumLengthValidator).
    page.fill('#password-change-form input[name="new_password"]', "12345")
    page.fill('#password-change-form input[name="new_password_confirm"]', "12345")
    page.click("#password-change-form button[type='submit']")

    expect(page.locator("#password-change-form .errorlist").first).to_be_visible()

    user.refresh_from_db()
    assert user.check_password(user_password)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_password_change_requires_login(live_server, page):
    """Anonymous user hitting /user/change-password is bounced to /log-in."""
    response = page.goto(f"{live_server.url}/user/change-password")
    assert response is not None
    # LOGIN_URL = /log-in; the redirect adds ?next=… as a query param.
    expect(page).to_have_url(
        re.compile(rf"^{re.escape(live_server.url)}/log-in(\?.*)?$")
    )
