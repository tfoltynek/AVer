"""Rename legacy selection_method keys to match the corrected Hitzinger mapping.

Originally I mis-labelled two trigram methods as bigrams ("adj_adv_bigram",
"noun_adj_bigram"). The actual Hitzinger calibration filter was
`word_counts == 3`, so those subgroups are trigrams. The new rules also
collapse all bigrams into a single `"bigram"` value (calibrated from the
CSV, n=2579).

Any existing row with the legacy keys was classified from 2-token content,
so it really belongs under the new single `"bigram"` key.
"""

from django.db import migrations


_RENAMES = {
    "adj_adv_bigram": "bigram",
    "noun_adj_bigram": "bigram",
}


def forwards(apps, schema_editor):
    Word = apps.get_model("core", "Word")
    for old, new in _RENAMES.items():
        Word.objects.filter(selection_method=old).update(selection_method=new)


def backwards(apps, schema_editor):
    # No clean inverse: we collapsed two keys into one, so we can't restore
    # the original split. Setting back to NULL forces a re-backfill.
    Word = apps.get_model("core", "Word")
    Word.objects.filter(selection_method="bigram").update(selection_method=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_alter_word_selection_method"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
