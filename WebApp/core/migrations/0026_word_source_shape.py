"""Split Word.word_selector into orthogonal `source` and `shape` fields.

The old field conflated three concepts: where the word came from (MUNI API
vs the local spaCy analyzer), the pick shape ("ml" secretly meant a
single-word MUNI pick with predictions), and, for local words, the pick
strategy (most_used_content_word / random). The new taxonomy:

- source: "muni_api" | "local_analyzer"
- shape: "unigram" | "bigram" | "trigram", derived from token count
- selection_method: unchanged for MUNI words (Hitzinger calibration
  bucket); local words now record their pick strategy here instead of in
  word_selector.

Forwards derives `shape` from content token count, NOT from the old label:
rows ingested before 2bed3f8 could store multi-token picks as "ml", and
legacy rows ingested before word_selector was stamped at all hold "" —
token count is ground truth for both. The empty-label rows are demo-corpus
MUNI ingests (many carry predictions), so they map to muni_api.

Backwards reconstructs word_selector almost fully: the ml-vs-unigram
distinction is recovered from the predictions JSONField. Intentional
non-roundtrips: legacy ""-labeled rows come back as ml/unigram/bigram/
trigram (corrective, matches what current ingest would write), and local
rows return their strategy to word_selector with selection_method reset to
NULL (its pre-migration state).
"""

from django.db import migrations, models

_LOCAL_STRATEGIES = {
    "most_used_content_word",
    "least_used_content_word",
    "random",
}


def _shape_from_content(content):
    n = len((content or "").split())
    if n == 2:
        return "bigram"
    if n == 3:
        return "trigram"
    return "unigram"  # 1 token, and (defensively) 0 or 4+, matching ingest


def forwards(apps, schema_editor):
    Word = apps.get_model("core", "Word")
    batch = []
    for word in Word.objects.all().iterator(chunk_size=500):
        if word.word_selector in _LOCAL_STRATEGIES:
            word.source = "local_analyzer"
            # Local rows never got a selection_method; the strategy lived
            # in word_selector. Fold it in.
            word.selection_method = word.word_selector
        else:
            word.source = "muni_api"
        word.shape = _shape_from_content(word.content)
        batch.append(word)
        if len(batch) >= 500:
            Word.objects.bulk_update(batch, ["source", "shape", "selection_method"])
            batch = []
    if batch:
        Word.objects.bulk_update(batch, ["source", "shape", "selection_method"])


def backwards(apps, schema_editor):
    Word = apps.get_model("core", "Word")
    Word.objects.filter(source="muni_api", shape="bigram").update(
        word_selector="bigram"
    )
    Word.objects.filter(source="muni_api", shape="trigram").update(
        word_selector="trigram"
    )
    Word.objects.filter(
        source="muni_api", shape="unigram", predictions__isnull=False
    ).update(word_selector="ml")
    Word.objects.filter(
        source="muni_api", shape="unigram", predictions__isnull=True
    ).update(word_selector="unigram")
    for strategy in _LOCAL_STRATEGIES:
        Word.objects.filter(
            source="local_analyzer", selection_method=strategy
        ).update(word_selector=strategy, selection_method=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_analysisjob_finished_at_analysisjob_started_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="word",
            name="source",
            field=models.CharField(
                choices=[
                    ("muni_api", "MUNI API"),
                    ("local_analyzer", "Local analyzer"),
                ],
                max_length=30,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="word",
            name="shape",
            field=models.CharField(
                choices=[
                    ("unigram", "Unigram"),
                    ("bigram", "Bigram"),
                    ("trigram", "Trigram"),
                ],
                max_length=30,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="word",
            name="selection_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ml_noun_unigram", "Unigram nouns (ML)"),
                    ("ml_adj_unigram", "Unigram adjectives (ML)"),
                    ("bigram", "Bigram (any)"),
                    ("adj_adv_trigram", "Trigrams w/ adjectives + adverbs"),
                    (
                        "noun_adj_adv_trigram",
                        "Trigrams w/ nouns + adjectives + adverbs",
                    ),
                    ("noun_adj_trigram", "Trigrams w/ nouns + adjectives"),
                    ("trigram_with_adj", "Trigrams containing adjectives"),
                    ("most_used_content_word", "most_used_content_word"),
                    ("least_used_content_word", "least_used_content_word"),
                    ("random", "random"),
                ],
                max_length=30,
                null=True,
            ),
        ),
        # Relax word_selector before the data migration so the whole file
        # reverses: the reverse of RemoveField re-adds it nullable, then
        # RunPython.backwards repopulates it, and only then does the
        # reverse of this AlterField restore NOT NULL. Without this step
        # the reverse crashes on SQLite's table rebuild (NULLs in a
        # NOT NULL column).
        migrations.AlterField(
            model_name="word",
            name="word_selector",
            field=models.CharField(
                choices=[
                    ("ml", "ML"),
                    ("unigram", "Unigram"),
                    ("bigram", "Bigram"),
                    ("trigram", "Trigram"),
                    ("most_used_content_word", "most_used_content_word"),
                    ("least_used_content_word", "least_used_content_word"),
                    ("random", "random"),
                ],
                max_length=30,
                null=True,
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name="word",
            name="source",
            field=models.CharField(
                choices=[
                    ("muni_api", "MUNI API"),
                    ("local_analyzer", "Local analyzer"),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="word",
            name="shape",
            field=models.CharField(
                choices=[
                    ("unigram", "Unigram"),
                    ("bigram", "Bigram"),
                    ("trigram", "Trigram"),
                ],
                max_length=30,
            ),
        ),
        migrations.RemoveField(
            model_name="word",
            name="word_selector",
        ),
    ]
