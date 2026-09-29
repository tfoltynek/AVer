from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from core.models import Test, TestedParagraph, Word
from core.tasks import dispatch_score_test


class Command(BaseCommand):
    help = "Clear computed_score on submitted tests and re-dispatch scoring."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--all", action="store_true", help="Every submitted test."
        )
        target.add_argument(
            "--since",
            type=str,
            help="Tests submitted on or after this ISO date (YYYY-MM-DD).",
        )
        target.add_argument(
            "--test-id", type=str, help="A single test by primary key."
        )
        parser.add_argument(
            "--reset-embeddings",
            action="store_true",
            help="Also clear Word.expected_embedding so they are re-encoded.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without writing or dispatching.",
        )

    def handle(self, *args, **opts):
        qs = Test.objects.filter(submitted_at__isnull=False)
        if opts["test_id"]:
            qs = qs.filter(pk=opts["test_id"])
        elif opts["since"]:
            since = parse_date(opts["since"])
            if not since:
                raise CommandError("--since must be YYYY-MM-DD")
            qs = qs.filter(submitted_at__date__gte=since)

        test_ids = list(qs.values_list("pk", flat=True))
        if not test_ids:
            self.stdout.write("No matching submitted tests.")
            return

        paragraphs = TestedParagraph.objects.filter(test_id__in=test_ids)
        paragraph_count = paragraphs.count()
        word_ids = (
            list(paragraphs.values_list("word_id", flat=True).distinct())
            if opts["reset_embeddings"]
            else []
        )

        self.stdout.write(
            f"Tests: {len(test_ids)}  Paragraphs: {paragraph_count}  "
            f"Words: {len(word_ids)}"
        )

        if opts["dry_run"]:
            self.stdout.write("Dry run — no changes made.")
            return

        paragraphs.update(computed_score=None, computed_grade="")
        if word_ids:
            Word.objects.filter(pk__in=word_ids).update(expected_embedding=None)

        for test_id in test_ids:
            dispatch_score_test(str(test_id))

        self.stdout.write(
            self.style.SUCCESS(
                f"Reset and dispatched scoring for {len(test_ids)} test(s)."
            )
        )
