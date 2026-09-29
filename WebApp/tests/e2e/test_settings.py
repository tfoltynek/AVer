"""E2E coverage for the settings page.

Originally scoped as a password-change flow, but the change-password URL is
currently commented out in core/urls.py pending a view refactor. Swapped for
the update-academic-fields HTMX partial flow, which is reachable from settings.
"""

import pytest
from playwright.sync_api import expect

from tests.e2e.pages import SettingsPage


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_update_academic_fields_flow(live_server, page, user_factory, login):
    user = user_factory(email="settings-user@example.com")
    login(user)

    settings = SettingsPage(page, live_server.url)
    settings.visit()

    settings.click_edit_academic_fields()
    page.wait_for_selector("#update-academic-fields-form")

    # Toggle a second field on. user_factory already seeded one field, so the
    # post-submit infobox should display two chips.
    settings.check_academic_field_by_index(1)
    settings.submit_academic_fields()

    # The success response renders the infobox via OOB swap.
    page.wait_for_selector("#academic-fields-infobox")
    expect(page.locator("#academic-fields-infobox .Chip")).to_have_count(2)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_update_academic_fields_rejects_empty_selection(
    live_server, page, user_factory, login
):
    """Submitting the dialog with zero fields must show a validation error
    and keep the dialog open."""
    user = user_factory(email="settings-empty@example.com")
    login(user)

    settings = SettingsPage(page, live_server.url)
    settings.visit()
    settings.click_edit_academic_fields()
    page.wait_for_selector("#update-academic-fields-form")

    # Uncheck the pre-seeded field, then submit.
    checked = page.locator(
        '#update-academic-fields-form input[name="academic_fields"]:checked'
    )
    for i in range(checked.count()):
        checked.nth(i).uncheck()
    settings.submit_academic_fields()

    # Form sticks around with an error; infobox is *not* swapped in.
    expect(page.locator("#update-academic-fields-form")).to_be_visible()
    expect(page.locator("#update-academic-fields-form .errorlist")).to_be_visible()
