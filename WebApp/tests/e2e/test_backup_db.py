"""What `backup_db` copies, when it declines to, and what it throws away.

The command exists so that a migration which rewrites rows can be undone by
restoring a file. That only works if the copy is taken before the migration
and if rotation never eats the copy that matters, so both are pinned here.
"""

import sqlite3
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections

from core.management.commands.backup_db import BACKUP_GLOB
from core.models import Document, Language

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


def _backups(directory):
    return sorted(p.name for p in directory.glob(BACKUP_GLOB))


def test_backup_is_a_readable_standalone_database(tmp_path):
    language = Language.objects.get(code="en")
    Document.objects.create(language=language, file="x.txt", title="x", publication_date="2026-01-01")

    out = StringIO()
    call_command("backup_db", "--dir", str(tmp_path), "--force", stdout=out)

    copies = _backups(tmp_path)
    assert len(copies) == 1
    assert "backed up to" in out.getvalue()

    # A single file, no -wal/-shm sidecar, and it opens on its own.
    assert sorted(p.name for p in tmp_path.iterdir()) == copies
    with sqlite3.connect(str(tmp_path / copies[0])) as restored:
        titles = [row[0] for row in restored.execute("select title from core_document")]
    assert "x" in titles


def test_nothing_pending_means_no_copy(tmp_path):
    # The test database is fully migrated, so without --force the command
    # must leave the previous copy alone: a deploy that changes no schema
    # must not rotate the pre-migration backup out of the window.
    out = StringIO()
    call_command("backup_db", "--dir", str(tmp_path), stdout=out)

    assert _backups(tmp_path) == []
    assert "no migrations pending" in out.getvalue()


def test_only_the_newest_copies_survive(tmp_path):
    for i in range(7):
        # Names carry a second-resolution timestamp, so write them directly
        # rather than racing the clock.
        (tmp_path / f"db-2026010{i}T000000Z.sqlite3").write_bytes(b"")

    out = StringIO()
    call_command("backup_db", "--dir", str(tmp_path), "--force", "--keep", "3", stdout=out)

    remaining = _backups(tmp_path)
    assert len(remaining) == 3
    # The fresh copy plus the two youngest placeholders; the five oldest go.
    assert remaining[0] == "db-20260105T000000Z.sqlite3"
    assert remaining[1] == "db-20260106T000000Z.sqlite3"
    assert remaining[2].startswith("db-") and remaining[2] not in {f"db-2026010{i}T000000Z.sqlite3" for i in range(7)}
    assert "removed old backup" in out.getvalue()


def test_keep_must_be_positive(tmp_path):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("backup_db", "--dir", str(tmp_path), "--force", "--keep", "0")


def test_a_non_sqlite_database_is_left_alone(tmp_path, monkeypatch):
    # `connection` is a proxy; shadow `vendor` on the wrapper behind it.
    monkeypatch.setattr(connections[DEFAULT_DB_ALIAS], "vendor", "postgresql")

    out = StringIO()
    call_command("backup_db", "--dir", str(tmp_path), "--force", stdout=out)

    assert _backups(tmp_path) == []
    assert "not sqlite" in out.getvalue()


def test_a_pending_plan_is_named_in_the_output(tmp_path, monkeypatch):
    # The suite always runs against a fully migrated database, so the loop
    # over the plan is never entered by the other tests. It was wrong for
    # that reason: it read `migration.key`, which Migration does not have,
    # and the deploy of 2026-09-25 died on it. Feed the command a plan of
    # real Migration objects so the attributes are checked against Django's
    # own class.
    from django.db.migrations import Migration

    from core.management.commands import backup_db

    class FakeExecutor:
        def __init__(self, connection):
            self.loader = type(
                "Loader", (), {"graph": type("Graph", (), {"leaf_nodes": staticmethod(lambda: [("core", "0029")])})}
            )()

        def migration_plan(self, targets):
            return [(Migration("0029_muni_random_unigrams", "core"), False)]

    monkeypatch.setattr(backup_db, "MigrationExecutor", FakeExecutor)

    out = StringIO()
    call_command("backup_db", "--dir", str(tmp_path), stdout=out)

    printed = out.getvalue()
    assert "1 migration(s) pending: core.0029_muni_random_unigrams" in printed
    assert "backed up to" in printed
    assert len(_backups(tmp_path)) == 1
