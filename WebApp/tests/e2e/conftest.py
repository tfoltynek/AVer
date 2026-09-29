"""Shared fixtures for the e2e suite.

Loads reference data (languages, academic fields, demo documents) once per
session. Provides factories for users and authenticated Playwright pages.
"""

from pathlib import Path

import pytest
from django.core.management import call_command
from django.test.testcases import LiveServerThread

from tests.e2e.live_server_support import JoiningWSGIServer

REPO_ROOT = Path(__file__).resolve().parents[2]

# pytest-django's session live server revokes the shared SQLite connection's
# thread sharing right after terminate(); the stock server would let a request
# thread finish its cleanup after that (see live_server_support).
LiveServerThread.server_class = JoiningWSGIServer

# The seeded-DB snapshot used by `serialized_rollback` restores. Kept here in
# addition to `connection._test_serialized_contents` because Django can hand
# out more than one DatabaseWrapper for the same alias within one session:
# asgiref's Local(thread_critical=True) switches from thread-local to
# contextvar storage the moment an asyncio loop runs in the thread, which is
# exactly what pytest-playwright's sync driver does when the session browser
# starts. A snapshot stored on the pre-loop wrapper is invisible on the
# post-loop wrapper, and serialized_rollback then silently restores nothing.
_serialized_seed: str | None = None


@pytest.fixture(scope="session", autouse=True)
def _browser_before_db(browser):
    """Launch the Playwright session browser before any DB fixture runs.

    pytest-playwright's sync driver starts an asyncio loop in this thread the
    moment the browser launches, which flips asgiref's Local storage from
    thread-local to contextvar and hands out a fresh DatabaseWrapper for the
    same alias. Forcing the launch first means every DB fixture (including
    django_db_setup's serialization and each test's serialized_rollback
    restore) runs against the post-loop wrapper, so the ambient connection
    never changes identity mid-session. Without this, the first browser test
    that runs after a non-browser transactional test restores nothing and
    starts from an empty DB.
    """
    return browser


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Seed the test DB with reference data needed by every flow.

    Languages + AcademicFields are required for signup and forms. Demo
    documents power the public try-mode flow.

    Django serializes the DB inside `setup_databases` (before this fixture's
    body runs), so seed data loaded here is NOT in the snapshot used by
    `serialized_rollback`. We re-serialize at the end so per-test flushes
    restore our seeded state instead of an empty DB.
    """
    global _serialized_seed
    from django.db import connection

    with django_db_blocker.unblock():
        call_command("loaddata", str(REPO_ROOT / "core/fixtures/languages.json"))
        call_command("loaddata", str(REPO_ROOT / "core/fixtures/academic_fields.json"))
        call_command("loaddocuments", str(REPO_ROOT / "test_documents/data"))
        call_command(
            "loaddocumentmetadata",
            str(REPO_ROOT / "test_documents/metadata.json"),
        )
        _serialized_seed = connection.creation.serialize_db_to_string()
        connection._test_serialized_contents = _serialized_seed


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Re-attach the seed snapshot to whatever connection is ambient.

    Runs before each test's fixtures, so the `serialized_rollback` restore in
    Django's `_fixture_setup` always finds `_test_serialized_contents`, even
    when the ambient DatabaseWrapper is not the one `django_db_setup` stamped
    (see the note on `_serialized_seed` above).
    """
    if _serialized_seed is None:
        return
    from django.db import connections

    # Unconditional and idempotent: nothing re-serializes after seeding, so
    # overwriting a stale value is always correct, and the hook self-heals if
    # some earlier test left a partial attribute behind. Only "default" needs
    # stamping; the suite uses a single DB alias.
    connections["default"]._test_serialized_contents = _serialized_seed


@pytest.fixture
def pending_ai_document(user_factory):
    """A Document with paragraphs and a pending 'ai' AnalysisJob, created
    directly in the ORM — for analysis-pipeline tests that don't need the
    upload UI."""
    import datetime

    from core.models import AnalysisJob, Document, Language, Paragraph

    owner = user_factory(email="analysis-owner@example.com")
    language = Language.objects.filter(code="en").first() or Language.objects.first()
    document = Document.objects.create(
        file="documents/pending-analysis.txt",
        title="Pending analysis fixture",
        language=language,
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=owner,
    )
    for i in range(3):
        Paragraph.objects.create(
            document=document,
            content=f"Analysis fixture paragraph number {i} with some content.",
            language=language,
        )
    AnalysisJob.objects.create(document=document, analysis_type="ai", status="pending")
    return document


@pytest.fixture
def user_password():
    return "e2e-test-pass-123!"


@pytest.fixture
def user_factory(db, user_password):
    """Create a User with a native language proficiency.

    The CheckNativeLanguageProficiencyMiddleware redirects authenticated users
    without a native proficiency to /user-languages; tests that want to land
    elsewhere need the proficiency pre-seeded.
    """
    from core.models import AcademicField, Language, LanguageProficiency, User

    created: list[User] = []

    def _make(email: str = "user@example.com", with_native: bool = True) -> User:
        user = User.objects.create(email=email, education="bachelors")
        user.set_password(user_password)
        user.save()
        field = AcademicField.objects.first()
        if field:
            user.academic_fields.add(field)
        if with_native:
            language = Language.objects.filter(code="en").first() or Language.objects.first()
            LanguageProficiency.objects.create(
                user=user, language=language, proficiency="native"
            )
        created.append(user)
        return user

    yield _make


@pytest.fixture
def login(live_server, page, user_password):
    """Return a helper that logs the given user in via the UI."""

    def _login(user):
        page.goto(f"{live_server.url}/log-in")
        page.fill('input[name="username"]', user.email)
        page.fill('input[name="password"]', user_password)
        page.click('button[type="submit"]')
        page.wait_for_url(f"{live_server.url}/dashboard")

    return _login
