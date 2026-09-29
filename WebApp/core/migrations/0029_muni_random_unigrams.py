"""Re-label the randomly removed single words ingested before `category`.

Until 2026-09-20 we derived a MUNI word's calibration bucket by POS-tagging
its content ourselves, under the rule "any unigram is an ML pick". The
service, however, tops a document up with words it removed at random when
its model finds too few candidates, and those are a different calibration
group: pA 0.70784 / pN 0.43586 instead of the 0.79506 / 0.53960 of an ML
noun unigram. Every such word has been scored as if the model had chosen
it, which inflates the evidence a correct answer carries.

New ingests take the group from the `category` the service reports. For the
rows already in the database there is no category to read, so this migration
uses the one signal those rows do carry: the service attaches `predictions`
to the words its model picked and leaves them empty on the ones it removed
at random.

Deliberately limited to `shape="unigram"`:

* Random picks can be multi-word — a trigram with `category: random` was
  observed on 2026-09-20 — so this does not catch all of them.
* But the old synchronous payloads carry no `predictions` on ML bigrams and
  trigrams either (all 16 multi-word words in `test_documents/data` have
  none), so for those shapes the signal cannot tell the two apart. Widening
  the filter would relabel genuine ML bigrams as random.

The residue is therefore multi-word random picks from before the change,
which keep their bigram/trigram bucket. Nothing in the stored data can
distinguish them; only a re-analysis could.

Irreversible by design: the previous value was a guess, so there is nothing
faithful to restore. Results pages recompute from `selection_method` on
every view, so the verdicts of affected historical tests shift when this
runs — that is the point.
"""

from django.db import migrations


def relabel_random_unigrams(apps, schema_editor):
    Word = apps.get_model("core", "Word")
    changed = (
        Word.objects.filter(
            source="muni_api",
            shape="unigram",
            predictions__isnull=True,
        )
        .exclude(selection_method="random")
        .update(selection_method="random")
    )
    if changed:
        print(f"  relabelled {changed} randomly removed unigram(s) as 'random'")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0028_word_muni_category"),
    ]

    operations = [
        migrations.RunPython(relabel_random_unigrams, migrations.RunPython.noop),
    ]
