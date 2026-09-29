"""E2E coverage for the /user-languages CRUD flow.

The user_factory seeds one native-English proficiency, so the page renders
with one card plus the "Add language" form. We exercise add → edit → delete.
"""

import pytest
from playwright.sync_api import expect


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_add_language_proficiency(live_server, page, user_factory, login):
    from core.models import Language, LanguageProficiency

    user = user_factory(email="polyglot@example.com")
    login(user)

    page.goto(f"{live_server.url}/user-languages")
    page.wait_for_selector("#user-languages")

    # Pick a language the user does NOT already have. user_factory seeded
    # English, so Czech is a safe pick (and the dropdown excludes EN).
    cs = Language.objects.get(code="cs")
    page.locator("#id_language").select_option(value=str(cs.pk))
    page.locator("#id_proficiency").select_option("B2")

    with page.expect_response(
        lambda r: r.url.endswith("/user-languages") and r.request.method == "POST"
    ):
        page.click("form .Button--accent:has-text('Add')")
    page.wait_for_load_state("networkidle")

    expect(
        page.locator('.UserLanguage-proficiency[data-proficiency="B2"]')
    ).to_be_visible()
    assert LanguageProficiency.objects.filter(
        user=user, language=cs, proficiency="B2"
    ).exists()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_edit_language_proficiency(live_server, page, user_factory, login):
    """Native EN is seeded by user_factory; bump it to C2 via the edit flow."""
    from core.models import Language, LanguageProficiency

    user = user_factory(email="edit-lang@example.com", with_native=False)
    cs = Language.objects.get(code="cs")
    proficiency = LanguageProficiency.objects.create(
        user=user, language=cs, proficiency="native"
    )

    login_url = f"{live_server.url}/log-in"
    page.goto(login_url)
    page.fill('input[name="username"]', user.email)
    page.fill('input[name="password"]', "e2e-test-pass-123!")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_server.url}/dashboard")

    page.goto(f"{live_server.url}/user-languages")
    # The Edit/Update controls are wired only through hx-post; without htmx
    # loaded, a fast click falls back to a native submit of the surrounding
    # form to the wrong endpoint. Wait until htmx is live.
    page.wait_for_function("() => window.htmx !== undefined")
    card_id = f"#lang-prof-{proficiency.pk}"
    page.click(f"{card_id} button:has-text('Edit')")
    page.wait_for_selector(f"form{card_id}")
    page.select_option(f"form{card_id} select[name='proficiency']", "C2")
    page.click(f"form{card_id} button[type='submit']:has-text('Update')")

    expect(
        page.locator(f"{card_id} .UserLanguage-proficiency")
    ).to_have_attribute("data-proficiency", "C2")


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_delete_language_proficiency(live_server, page, user_factory, login):
    """Remove the seeded native-EN row via the edit form's Remove button."""
    from core.models import LanguageProficiency

    user = user_factory(email="del-lang@example.com")
    proficiency = LanguageProficiency.objects.get(user=user)
    card_id = f"#lang-prof-{proficiency.pk}"

    login(user)
    page.goto(f"{live_server.url}/user-languages")
    # See test_edit_language_proficiency: the Remove button is hx-post-only,
    # so a click before htmx executes becomes a native submit to the wrong
    # endpoint while the navigation blanks the DOM, letting the count-0
    # expectation pass without any delete having happened.
    page.wait_for_function("() => window.htmx !== undefined")
    expect(page.locator(card_id)).to_be_visible()

    page.click(f"{card_id} button:has-text('Edit')")
    page.wait_for_selector(f"form{card_id}")
    # Wait for the delete POST itself, so the DB assertion below can't race
    # the server-side commit.
    with page.expect_response(
        lambda r: "/user-language" in r.url and r.request.method == "POST"
    ):
        page.click(f"form{card_id} button:has-text('Remove')")

    expect(page.locator(card_id)).to_have_count(0)
    assert not LanguageProficiency.objects.filter(pk=proficiency.pk).exists()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_delete_other_users_language_proficiency_is_blocked(
    live_server, page, user_factory, login
):
    """Two safety nets: the URL is POST-only (GET 405s, CSRF-safe), and the
    POST handler scopes by request.user (cross-user 404s)."""
    from core.models import LanguageProficiency

    victim = user_factory(email="victim@example.com")
    attacker = user_factory(email="attacker@example.com")
    victim_prof = LanguageProficiency.objects.get(user=victim)

    login(attacker)

    # GET is no longer a valid method — protects against drive-by deletes via
    # <img src> or pre-fetched links.
    get_response = page.goto(
        f"{live_server.url}/user-languages/{victim_prof.pk}/delete"
    )
    assert get_response is not None and get_response.status == 405
    assert LanguageProficiency.objects.filter(pk=victim_prof.pk).exists()

    # Even an authenticated POST from the attacker hits the user-scoped
    # get_object_or_404 and 404s, leaving the row intact.
    csrf = page.context.cookies(live_server.url)
    csrf_value = next(c["value"] for c in csrf if c["name"] == "csrftoken")
    post_response = page.context.request.post(
        f"{live_server.url}/user-languages/{victim_prof.pk}/delete",
        headers={"X-CSRFToken": csrf_value},
    )
    assert post_response.status == 404
    assert LanguageProficiency.objects.filter(pk=victim_prof.pk).exists()
