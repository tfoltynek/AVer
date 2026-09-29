from datetime import UTC, datetime

import pytest
from playwright.sync_api import expect

from core.models import Document, Language

UPLOADED_AT = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)


def _seed_document(user):
    """Create one document owned by `user` with a fixed UTC uploaded_at."""
    lang = Language.objects.first()
    doc = Document.objects.create(
        title="TZ Doc",
        language=lang,
        publication_date="2024-01-01",
        uploaded_by=user,
    )
    # uploaded_at is auto_now_add; .update() bypasses it to set a known value.
    Document.objects.filter(pk=doc.pk).update(uploaded_at=UPLOADED_AT)
    return doc


def _login(page, live_server, user, password):
    page.goto(f"{live_server.url}/log-in")
    page.fill('input[name="username"]', user.email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_server.url}/dashboard")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_uploaded_at_emitted_as_utc_time_element(
    live_server, page, user_factory, user_password
):
    user = user_factory(email="tz-attr@example.com")
    _seed_document(user)
    _login(page, live_server, user, user_password)
    page.goto(f"{live_server.url}/documents")
    el = page.locator(".js-localdt").first
    expect(el).to_have_attribute("datetime", "2026-06-01T14:30:00+00:00")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.parametrize(
    "tz_id,expected",
    [
        ("Europe/Prague", "01.06.2026 16:30"),  # CEST = UTC+2 in June
        ("America/New_York", "01.06.2026 10:30"),  # EDT = UTC-4 in June
    ],
)
def test_uploaded_at_rendered_in_browser_timezone(
    live_server, browser, user_factory, user_password, tz_id, expected
):
    user = user_factory(email="tz-shift@example.com")
    _seed_document(user)

    context = browser.new_context(timezone_id=tz_id)
    page = context.new_page()
    try:
        _login(page, live_server, user, user_password)
        page.goto(f"{live_server.url}/documents")
        expect(page.locator(".js-localdt").first).to_have_text(expected)
    finally:
        context.close()
