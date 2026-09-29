"""Backfill `Word.selection_method` for rows ingested before the field existed.

Re-POS-tags the existing `content` (no MUNI API re-call) and writes the
granular Hitzinger method. Only MUNI-sourced rows qualify: local-analyzer
rows always carry their pick strategy as the method. Idempotent and safe
to re-run: skips rows that already have a method, and ignores languages
without a stanza model.

Since ingestion takes the bucket from the service's `category`, this is a
command for the rows recorded before that field was read. Rows that do
carry a category are left alone even when their method is NULL: that NULL
is a decision (a category we understand but have no calibration for), and
POS-tagging would overwrite it with a bucket the word does not belong to.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Word
from core.utils.pos_method import SUPPORTED_LANGUAGES, classify_method


class Command(BaseCommand):
    help = (
        "Backfill Word.selection_method by POS-tagging stored content. "
        "Skips rows that already have a method, rows that carry a MUNI "
        "selection category, and languages with no stanza POS model."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows to stream + commit per transaction (default 500).",
        )
        parser.add_argument(
            "--language",
            help="Restrict to one Document.language.code (e.g. 'cs').",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **opts):
        batch_size = opts["batch_size"]
        language = opts.get("language")
        dry_run = opts["dry_run"]

        qs = (
            Word.objects.select_related("paragraph__document__language")
            .filter(
                selection_method__isnull=True,
                source=Word.Source.MUNI_API,
                # A stored category with no method means "understood, not
                # calibrated"; stanza must not guess a bucket over it.
                muni_category__isnull=True,
            )
            .order_by("pk")
        )
        if language:
            qs = qs.filter(paragraph__document__language__code=language)

        total = qs.count()
        self.stdout.write(
            f"backfill_selection_method: {total} candidate words"
            + (" [dry-run]" if dry_run else "")
        )

        processed = 0
        updated = 0
        skipped_language = 0
        skipped_unclassified = 0
        pending: list[Word] = []

        def flush():
            nonlocal pending
            if not pending or dry_run:
                pending = []
                return
            with transaction.atomic():
                Word.objects.bulk_update(pending, ["selection_method"])
            pending = []

        for word in qs.iterator(chunk_size=batch_size):
            language_code = word.paragraph.document.language.code
            if language_code not in SUPPORTED_LANGUAGES:
                skipped_language += 1
                processed += 1
                continue

            method = classify_method(
                content=word.content or "",
                language_code=language_code,
            )
            if method is None:
                skipped_unclassified += 1
                processed += 1
                continue

            word.selection_method = method
            pending.append(word)
            updated += 1
            processed += 1

            if len(pending) >= batch_size:
                flush()
                self.stdout.write(
                    f"  …{processed}/{total} "
                    f"(updated={updated}, no-lang={skipped_language}, "
                    f"no-match={skipped_unclassified})"
                )

        flush()

        self.stdout.write(
            self.style.SUCCESS(
                f"done. updated={updated}, "
                f"skipped(no stanza lang)={skipped_language}, "
                f"skipped(no POS match)={skipped_unclassified}, "
                f"total={total}"
            )
        )
