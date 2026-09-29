"""Pre-download stanza tokenize+pos models for every supported language.

Idempotent: stanza's downloader is a no-op when the resources are already
present, so this is safe to run on every deploy or container startup.
Used by the Dockerfile to bake models into the image and by `setup_db.sh`
so a fresh dev clone has everything ready for `perform_ai_analysis`.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from core.utils.pos_method import SUPPORTED_LANGUAGES, ensure_model


class Command(BaseCommand):
    help = "Download stanza POS models for the languages we tag at ingestion."

    def add_arguments(self, parser):
        parser.add_argument(
            "languages",
            nargs="*",
            help=(
                "Language codes to fetch (default: all supported). "
                f"Supported: {', '.join(sorted(SUPPORTED_LANGUAGES))}."
            ),
        )

    def handle(self, *args, **opts):
        targets = opts.get("languages") or sorted(SUPPORTED_LANGUAGES)
        ok, failed = [], []
        for lang in targets:
            self.stdout.write(f"  fetching {lang}…", ending=" ")
            self.stdout.flush()
            if ensure_model(lang):
                ok.append(lang)
                self.stdout.write(self.style.SUCCESS("ok"))
            else:
                failed.append(lang)
                self.stdout.write(self.style.ERROR("failed"))

        if failed:
            self.stdout.write(
                self.style.WARNING(
                    f"done. ok={ok} failed={failed} (scoring will fall back to "
                    f"bucket averages for failed languages)"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"done. ok={ok}"))
