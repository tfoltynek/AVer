"""E2E coverage for the /tests list page.

Only the requesting user's own tests should render. Try-mode tests
(user IS NULL) and other users' tests must stay hidden.
"""

import pytest
from playwright.sync_api import expect


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_test_list_shows_only_own_tests(live_server, page, user_factory, login):
    """Three rows in the DB: one for me, one for someone else, one anonymous.
    Only mine should be visible on /tests."""
    from core.models import Document, Language, Test

    me = user_factory(email="me@example.com")
    other = user_factory(email="other@example.com")

    # Re-use any seeded language; documents need a non-null FK.
    lang = Language.objects.first()
    my_doc = Document.objects.create(
        title="my doc", language=lang, publication_date="2024-01-01"
    )
    other_doc = Document.objects.create(
        title="other doc", language=lang, publication_date="2024-01-01"
    )
    try_doc = Document.objects.create(
        title="try doc", language=lang, publication_date="2024-01-01"
    )

    Test.objects.create(document=my_doc, user=me, type="authorML")
    Test.objects.create(document=other_doc, user=other, type="authorML")
    Test.objects.create(document=try_doc, user=None, type="try")

    login(me)
    page.goto(f"{live_server.url}/tests")

    expect(page.locator(".TestCard")).to_have_count(1)
    expect(page.locator(".TestCard-title")).to_have_text("my doc")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_test_list_empty_state(live_server, page, user_factory, login):
    """No tests yet → only the 'Start new test' CreateCard is rendered."""
    user = user_factory(email="empty-list@example.com")
    login(user)

    page.goto(f"{live_server.url}/tests")
    expect(page.locator(".TestCard")).to_have_count(0)
    expect(page.locator(".CreateCard")).to_be_visible()
