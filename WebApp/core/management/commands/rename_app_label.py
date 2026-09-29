"""One-shot: move this app's persisted identity from the ``tests`` label to ``core``.

Renaming the Django app package is not enough. Three things in the database still
carry the old label and none of them are reachable by a normal migration:

* the 14 table names, all prefixed ``tests_``;
* ``django_migrations.app``, which is how the migration loader decides what has
  already been applied — leave it and Django sees ``core`` as a brand-new app and
  happily re-creates every table, empty;
* ``django_content_type.app_label``, which ``auth_permission`` and
  ``django_admin_log`` point at by id;
* the auto-created foreign-key indexes, whose names Django derives from the
  table name. SQLite carries them across a table rename with the old name
  intact, and Django never reads those names back, so they are cosmetic --
  but leaving them would keep ``tests_`` alive in the schema, which is the
  one thing this rename exists to stop.

So this runs *before* ``migrate``, not as part of it. It is idempotent: if the
rename already happened it reports so and does nothing.

    manage.py rename_app_label            # tests -> core
    manage.py rename_app_label --reverse  # core -> tests, for rollback
"""

from pathlib import Path

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

OLD_LABEL = "tests"
NEW_LABEL = "core"

# Indexes declared in a model's ``Meta.indexes`` are tracked *by name* in
# migration state, so migration 0027 renames this one itself. Touching it here
# would leave that migration's RenameIndex with no old name to find. Both
# spellings are listed so --reverse skips it too.
MIGRATION_MANAGED_INDEXES = {
    "tests_analy_created_44c500_idx",
    "core_analys_created_9f3b7d_idx",
}


def table_suffixes():
    """The part of each table name after the app-label prefix.

    Derived from the app registry rather than hard-coded so it cannot drift.
    ``include_auto_created`` is what picks up the two many-to-many through
    tables, which have no model of their own.
    """
    config = apps.get_app_config(NEW_LABEL)
    prefix = f"{NEW_LABEL}_"
    suffixes = []
    for model in config.get_models(include_auto_created=True):
        table = model._meta.db_table
        if not table.startswith(prefix):
            raise CommandError(
                f"{model._meta.label} has an explicit db_table ({table!r}); "
                "this command only understands label-prefixed tables."
            )
        suffixes.append(table[len(prefix) :])
    return sorted(suffixes)


def snapshot_database(target):
    """Copy the database aside before touching it, once, on the way in.

    Deployment is automatic (GitLab pushes to main and the postdeploy job runs
    this), so there is no operator standing by to take a backup first. Uses
    SQLite's online backup API rather than a file copy so it stays consistent
    against a live writer. Returns the path written, or None.
    """
    if connection.vendor != "sqlite":
        return None
    db_path = Path(connection.settings_dict["NAME"])
    if not db_path.is_file():
        return None
    destination = db_path.with_name(f"{db_path.stem}.pre-{target}-rename.sqlite3")
    if destination.exists():
        return destination  # a previous attempt already snapshotted; keep the oldest
    connection.ensure_connection()
    import sqlite3

    backup = sqlite3.connect(destination)
    try:
        connection.connection.backup(backup)
    finally:
        backup.close()
    return destination


def rename_indexes(cursor, source, target):
    """Re-point index names at the new label.

    SQLite cannot rename an index, so each one is dropped and recreated from
    its own stored DDL with only the name swapped. The table name inside that
    DDL was already rewritten by ``ALTER TABLE ... RENAME``. Implicit indexes
    (``sqlite_autoindex_*``, ``sql IS NULL``) follow their table on their own
    and are left alone.
    """
    if connection.vendor != "sqlite":
        return []

    declared = sum(
        len(model._meta.indexes) for model in apps.get_app_config(NEW_LABEL).get_models(include_auto_created=True)
    )
    if declared != 1:
        raise CommandError(
            f"{declared} Meta.indexes declared but MIGRATION_MANAGED_INDEXES "
            "covers one. A model gained a declared index; add its pre- and "
            "post-rename names there before running this."
        )

    cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
    prefix = f"{source}_"
    stale = [row for row in cursor.fetchall() if row[0].startswith(prefix) and row[0] not in MIGRATION_MANAGED_INDEXES]
    renamed = []
    for old_name, sql in stale:
        new_name = f"{target}_{old_name[len(prefix) :]}"
        cursor.execute(f'DROP INDEX "{old_name}"')
        cursor.execute(sql.replace(old_name, new_name, 1))
        renamed.append((old_name, new_name))
    return renamed


class Command(BaseCommand):
    help = "Rewrite table names and Django metadata from the 'tests' app label to 'core'."

    # Must run against a database Django currently considers inconsistent, so
    # neither system checks nor the unapplied-migration warning may gate it.
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--reverse",
            action="store_true",
            help="Undo the rename (core -> tests).",
        )

    def handle(self, *args, **options):
        source, target = (NEW_LABEL, OLD_LABEL) if options["reverse"] else (OLD_LABEL, NEW_LABEL)
        suffixes = table_suffixes()
        existing = set(connection.introspection.table_names())

        still_old = [s for s in suffixes if f"{source}_{s}" in existing]
        already_new = [s for s in suffixes if f"{target}_{s}" in existing]

        # A table absent under both names is not an error: the database is
        # simply behind on migrations and `migrate` will create it, correctly
        # named, straight after this. Only a mix of the two prefixes means
        # something renamed half of it and stopped.
        if still_old and already_new:
            raise CommandError(
                "Refusing to run on a half-renamed database: "
                f"{', '.join(already_new)} already use the '{target}' prefix "
                f"while {', '.join(still_old)} still use '{source}'."
            )

        if not still_old:
            self.stdout.write(
                self.style.SUCCESS(f"Nothing to do: no {source}_* tables present, {target}_* is already the schema.")
            )
            return

        pending = [(f"{source}_{s}", f"{target}_{s}") for s in still_old]

        snapshot = snapshot_database(target)
        if snapshot:
            self.stdout.write(f"Snapshot written to {snapshot}")

        with transaction.atomic(), connection.cursor() as cursor:
            for old_table, new_table in pending:
                cursor.execute(f'ALTER TABLE "{old_table}" RENAME TO "{new_table}"')
                self.stdout.write(f"  {old_table} -> {new_table}")

            cursor.execute(
                "UPDATE django_migrations SET app = %s WHERE app = %s",
                [target, source],
            )
            migrations_moved = cursor.rowcount

            cursor.execute(
                "UPDATE django_content_type SET app_label = %s WHERE app_label = %s",
                [target, source],
            )
            content_types_moved = cursor.rowcount

            renamed_indexes = rename_indexes(cursor, source, target)
            for old_name, new_name in renamed_indexes:
                self.stdout.write(f"  {old_name} -> {new_name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Renamed {len(pending)} tables, {len(renamed_indexes)} indexes, "
                f"{migrations_moved} django_migrations rows and "
                f"{content_types_moved} django_content_type rows "
                f"from '{source}' to '{target}'."
            )
        )
