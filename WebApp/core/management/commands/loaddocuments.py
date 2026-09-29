import json
import os
from datetime import date

from django.core.management.base import BaseCommand

from core.models import Document, Paragraph, Word
from core.tasks import _classify_returned_word


class Command(BaseCommand):
    help = "Load all JSON documents from a directory"

    def add_arguments(self, parser):
        parser.add_argument(
            "json_dir",
            type=str,
            help="Path to the directory containing the JSON files.",
        )

    def handle(self, *args, **kwargs):
        json_dir = kwargs["json_dir"]

        # Check if the directory exists
        if not os.path.isdir(json_dir):
            self.stdout.write(self.style.ERROR(f"Directory {json_dir} does not exist."))
            return

        # Request file name -> the Document it created, so the response pass
        # can scope its paragraph lookups to the right document.
        documents_by_request: dict[str, Document] = {}

        # Iterate over all files in the directory
        for filename in os.listdir(json_dir):
            if filename.endswith("request.json"):
                file_path = os.path.join(json_dir, filename)

                try:
                    with open(file_path, encoding="utf-8") as f:
                        data = json.load(f)

                    # Create or get the document
                    document = Document.objects.create(
                        language_id=data["language"],
                        file=filename,
                        publication_date=date.today(),
                        title=filename,
                    )
                    documents_by_request[filename] = document

                    # Create paragraphs
                    for paragraph in data["paragraphs"]:
                        Paragraph.objects.create(
                            document=document,
                            content=paragraph["content"],
                            language_id=data["language"],
                        )

                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Document {data["document_id"]} loaded from {filename}.'
                        )
                    )

                except json.JSONDecodeError:
                    self.stdout.write(
                        self.style.ERROR(f"Invalid JSON in file {filename}, skipping.")
                    )

                except KeyError as e:
                    self.stdout.write(
                        self.style.ERROR(
                            f"Missing key {e} in file {filename}, skipping."
                        )
                    )

        for filename in os.listdir(json_dir):
            if filename.endswith("response.json"):
                file_path = os.path.join(json_dir, filename)

                try:
                    with open(file_path, encoding="utf-8") as f:
                        data = json.load(f)

                    document = documents_by_request.get(
                        filename.replace("response.json", "request.json")
                    )
                    if document is None:
                        self.stdout.write(
                            self.style.WARNING(
                                f"{filename}: no document loaded for it, skipping."
                            )
                        )
                        continue

                    for word_data in data["words"]:
                        words = word_data["content"].split(" ")
                        full_sentence: str = word_data["sentence_blanked"]

                        for word in words:
                            full_sentence = full_sentence.replace("<<BLANK>>", word, 1)

                        # Match the word back to its paragraph by text: the
                        # paragraph ids we sent are not persisted. Scoped to this
                        # file's document, or a fragment could match a paragraph
                        # of another demo text. Try the tail after the first
                        # blank, then the head before it, then the filled-in
                        # sentence.
                        in_document = Paragraph.objects.filter(document=document)
                        blanked = word_data["sentence_blanked"]
                        candidates = in_document.filter(
                            content__contains=blanked.split("<<BLANK>>")[1]
                        )
                        if len(candidates) != 1:
                            candidates = in_document.filter(
                                content__contains=blanked.split("<<BLANK>>")[0]
                            )
                        if len(candidates) != 1:
                            candidates = in_document.filter(
                                content__contains=full_sentence
                            )
                        if len(candidates) == 0:
                            # Never an exception: this command seeds the whole
                            # e2e session, so one unmatched word must not abort
                            # the rest of the corpus.
                            self.stdout.write(
                                self.style.WARNING(
                                    f"{filename}: no paragraph matches "
                                    f"{word_data['content']!r}, skipping the word."
                                )
                            )
                            continue
                        paragraph = candidates[0]

                        # Demo words bypass the MUNI ingestion path, so classify
                        # them the same way it does. Without this they land with
                        # selection_method=NULL and every one of them is scored
                        # with the fallback pA/pN instead of its own bucket, so
                        # try-mode verdicts would not match the real thing.
                        classified = _classify_returned_word(
                            word_data, paragraph.language_id
                        )
                        word = Word.objects.create(
                            paragraph=paragraph,
                            content=word_data["content"],
                            index=word_data["index"],
                            sentence_index=word_data["sentence_index"],
                            sentence_blanked=word_data["sentence_blanked"],
                            predictions=word_data["predictions"]
                            if word_data["predictions"]
                            else None,
                            source=classified["source"],
                            shape=classified["shape"],
                            selection_method=classified["selection_method"],
                            muni_category=classified["muni_category"],
                            muni_origin_category=classified["muni_origin_category"],
                            muni_pos_tags=classified["muni_pos_tags"],
                        )
                except json.JSONDecodeError:
                    self.stdout.write(
                        self.style.ERROR(f"Invalid JSON in file {filename}, skipping.")
                    )

                except KeyError as e:
                    self.stdout.write(
                        self.style.ERROR(
                            f"Missing key {e} in file {filename}, skipping."
                        )
                    )

        # self.stdout.write(self.style.SUCCESS("All JSON files processed successfully."))
