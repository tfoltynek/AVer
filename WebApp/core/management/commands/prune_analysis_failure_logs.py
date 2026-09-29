"""Drop AnalysisFailureLog rows older than the retention window.

Idempotent; safe to run on a cron or manually. The default 60-day window
matches the working assumption (~2 months retention) — override with
--days for shorter / longer retention.

Examples:
    python manage.py prune_analysis_failure_logs
    python manage.py prune_analysis_failure_logs --days 30
    python manage.py prune_analysis_failure_logs --dry-run
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import AnalysisFailureLog

DEFAULT_RETENTION_DAYS = 60


class Command(BaseCommand):
    help = (
        f"Delete AnalysisFailureLog rows older than --days "
        f"(default {DEFAULT_RETENTION_DAYS})."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report counts without deleting.",
        )

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=opts["days"])
        qs = AnalysisFailureLog.objects.filter(created_at__lt=cutoff)
        count = qs.count()

        if opts["dry_run"]:
            self.stdout.write(
                f"would delete {count} AnalysisFailureLog rows older than {cutoff}"
            )
            return

        deleted, _ = qs.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"deleted {deleted} AnalysisFailureLog rows older than {cutoff}"
            )
        )
