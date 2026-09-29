import json
import os

from django.core.management.base import BaseCommand

from core.models import Document


class Command(BaseCommand):
    help = "Load all JSON documents from a directory"

    def add_arguments(self, parser):
        parser.add_argument(
            "json_file",
            type=str,
            help="Path to the JSON metadata file.",
        )

    def handle(self, *args, **kwargs):
        json_file = kwargs["json_file"]

        # Check if the directory exists
        if not os.path.exists(json_file):
            self.stdout.write(self.style.ERROR(f"{json_file} does not exist."))
            return

        # Iterate over all files in the directory
        file_path = os.path.join(json_file)

        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)

            for file_name, document_data in data.items():
                # Create or get the document
                document = Document.objects.get(title=file_name)
                document.title = document_data.get("title")
                document.author = document_data.get("author")
                document.released_at = document_data.get("year")
                document.save()

                self.stdout.write(
                    self.style.SUCCESS(f"Document {document.file} updated.")
                )

        except json.JSONDecodeError:
            self.stdout.write(
                self.style.ERROR(f"Invalid JSON in file {file_name}, skipping.")
            )

        except KeyError as e:
            self.stdout.write(
                self.style.ERROR(f"Missing key {e} in file {file_name}, skipping.")
            )
