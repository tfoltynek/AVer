"""E2E coverage for the public /data page.

The page publishes the project's user-study dataset as a static CSV. Anyone
can open the page without logging in, and the download link must resolve to
the file itself with its header row intact — a broken static reference would
otherwise only show up after a deploy.
"""

import csv
import io
from pathlib import Path
from urllib.parse import urljoin

import pytest
from playwright.sync_api import expect

# Distributed separately from the source code (see README), so a checkout
# may not have it.
DATASET = Path(__file__).resolve().parents[2] / "core/static/core/data/aver-user-studies-responses.csv"

EXPECTED_COLUMNS = [
    "test_id",
    "test_type",
    "word_types",
    "similarity",
    "answered",
    "content",
    "sentence",
    "selector",
    "ml_pos",
    "prob",
    "log_prob",
    "Language",
    "lang_code",
    "lang_code_2",
]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_data_page_is_public(live_server, page):
    page.goto(f"{live_server.url}/data")
    expect(page).to_have_url(f"{live_server.url}/data")
    expect(page.get_by_role("heading", name="Responses from user studies")).to_be_visible()
    # The column glossary names the <<BLANK>> marker literally; a translated
    # string literal is marked safe, so an unescaped one would render as "<>".
    page.locator("details").evaluate("d => { d.open = true }")
    expect(page.get_by_text("<<BLANK>>")).to_be_visible()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.parametrize(
    ("language", "heading"),
    [
        ("cs", "Odpovědi respondentů z uživatelských studií"),
        ("sk", "Odpovede respondentov z používateľských štúdií"),
    ],
)
def test_data_page_is_translated(live_server, page, language, heading):
    """The page is public-facing, so a stale .mo (strings added to the
    catalog but never compiled) would ship English to Czech readers."""
    page.set_extra_http_headers({"Accept-Language": language})
    page.goto(f"{live_server.url}/data")
    expect(page.get_by_role("heading", name=heading)).to_be_visible()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_download_link_reachable_on_a_phone(live_server, page):
    """The no-sidebar layout keeps the footer on screen and scrolls the main
    column internally; on a phone that column is short, so the card must
    still scroll into view rather than be clipped away."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server.url}/data")
    link = page.get_by_role("link", name="Download CSV")
    link.scroll_into_view_if_needed()
    expect(link).to_be_in_viewport()


@pytest.mark.skipif(not DATASET.exists(), reason="dataset file is not in this checkout")
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_dataset_download_link_serves_the_csv(live_server, page):
    page.goto(f"{live_server.url}/data")
    link = page.get_by_role("link", name="Download CSV")
    expect(link).to_be_visible()
    expect(link).to_have_attribute("download", "aver-user-studies-responses.csv")

    response = page.request.get(urljoin(live_server.url, link.get_attribute("href")))
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/csv")
    header = next(csv.reader(io.StringIO(response.text())))
    assert header == EXPECTED_COLUMNS
