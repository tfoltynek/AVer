"""Copy the database aside before a migration changes it.

A migration that rewrites rows cannot be undone by running it backwards:
0029, for one, relabels words on evidence that no longer exists afterwards,
so its reverse is a no-op. The safety net is a copy taken *before* the
migration, which is what this command makes.

It runs in the deploy pipeline ahead of `migrate` and takes a copy only when
there is something to apply, so a deploy that changes no schema leaves the
backups alone and the newest one stays the pre-migration state it was taken
for. The last `--keep` copies are retained, oldest first out.

The copy is made with SQLite's online backup API rather than by copying the
file: the web and worker containers share the database, so a plain copy
could catch a half-written transaction or miss the write-ahead log. The
result is a single self-contained file with no `-wal` or `-shm` sidecar.

Restoring one is a file move with the services down:

    docker compose stop aver celery
    docker compose run --rm --entrypoint sh aver -c \\
        'cp /app/db/backups/db-<stamp>.sqlite3 /app/db/db.sqlite3'
    docker compose start aver celery

The copies live in the same volume as the database, which protects against a
bad migration but not against losing the volume. Off-host backup, if it is
wanted, is a separate job.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BACKUP_GLOB = "db-*.sqlite3"
DEFAULT_KEEP = 5


class Command(BaseCommand):
    help = "Back up the SQLite database before migrating, keeping the last few copies."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            type=int,
            default=DEFAULT_KEEP,
            help=f"How many copies to retain (default {DEFAULT_KEEP}).",
        )
        parser.add_argument(
            "--dir",
            help="Where to write them (default: a `backups` folder beside the database).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Copy even when no migration is pending.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "sqlite":
            self.stdout.write(
                self.style.WARNING(
                    f"database is {connection.vendor}, not sqlite; nothing to do"
                )
            )
            return

        keep = options["keep"]
        if keep < 1:
            raise CommandError("--keep must be at least 1")

        pending = self.pending_migrations()
        if not pending and not options["force"]:
            self.stdout.write("no migrations pending, keeping the backups as they are")
            return

        target_dir = self.target_dir(options.get("dir"))
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / self.filename()

        if pending:
            self.stdout.write(
                f"{len(pending)} migration(s) pending: "
                + ", ".join(f"{app}.{name}" for app, name in pending)
            )
        self.copy_to(destination)
        size_mb = destination.stat().st_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f"backed up to {destination} ({size_mb:.1f} MB)"))

        for dropped in self.rotate(target_dir, keep):
            self.stdout.write(f"removed old backup {dropped.name}")

    # ── steps ───────────────────────────────────────────────────────────

    def pending_migrations(self) -> list[tuple[str, str]]:
        """The (app_label, name) pairs `migrate` would apply right now."""
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        # A plan entry is a Migration instance, which carries the two halves
        # of its node key separately; there is no `key` attribute on it.
        return [
            (migration.app_label, migration.name)
            for migration, _backwards in executor.migration_plan(targets)
        ]

    def target_dir(self, given: str | None) -> Path:
        if given:
            return Path(given)
        name = connection.settings_dict["NAME"]
        if name in {":memory:", ""} or str(name).startswith("file:"):
            raise CommandError(
                "the database is in memory; pass --dir to say where backups go"
            )
        return Path(name).parent / "backups"

    def filename(self) -> str:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"db-{stamp}.sqlite3"

    def copy_to(self, destination: Path) -> None:
        connection.ensure_connection()
        target = sqlite3.connect(str(destination))
        try:
            with target:
                connection.connection.backup(target)
        finally:
            target.close()

    def rotate(self, directory: Path, keep: int) -> list[Path]:
        """Delete all but the newest `keep` copies; return what went."""
        copies = sorted(directory.glob(BACKUP_GLOB))
        dropped = copies[:-keep] if len(copies) > keep else []
        for path in dropped:
            path.unlink()
        return dropped
