"""E2E coverage for anonymous-user access control.

Pages protected by LoginRequiredMixin should redirect unauthenticated
visitors to /log-in. Try-mode pages and the landing page stay public.
"""

import re

import pytest
from playwright.sync_api import expect

PROTECTED_PATHS = [
    "/dashboard",
    "/documents",
    "/documents/upload",
    "/settings",
    "/tests",
    "/user-languages",
]

PUBLIC_PATHS = [
    "/",
    "/log-in",
    "/sign-up",
    "/try/documents/cs",
]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_anonymous_user_redirected_to_login(live_server, page, path):
    page.goto(f"{live_server.url}{path}")
    # Login-protected views send anonymous users to /log-in with ?next=…;
    # the path itself becomes the value of next, so a prefix match is enough.
    expect(page).to_have_url(
        re.compile(rf"^{re.escape(live_server.url)}/log-in(\?.*)?$")
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_anonymous_user_can_reach_public_pages(live_server, page, path):
    response = page.goto(f"{live_server.url}{path}")
    assert response is not None
    # Public pages must respond 2xx; redirects (3xx) are a regression here.
    assert 200 <= response.status < 300, (
        f"{path} returned {response.status}; expected 2xx for an anonymous visitor"
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_authenticated_landing_page_redirects_to_dashboard(
    live_server, page, user_factory, login
):
    """`RestrictAuthenticatedUserMiddleware` bounces logged-in users away from
    the landing page, /log-in and /sign-up."""
    user = user_factory(email="redirect-me@example.com")
    login(user)

    for path in ("/", "/log-in", "/sign-up"):
        page.goto(f"{live_server.url}{path}")
        expect(page).to_have_url(f"{live_server.url}/dashboard")
