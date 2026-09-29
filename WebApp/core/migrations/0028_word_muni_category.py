"""Keep what the MUNI service says about each word it picked.

The async API returns three fields per selected word that we used to drop on
the floor: `category` ("unigrams_NOUN", "trigrams_w_ADJ", "random", …), which
names the cloze class the word was extracted from; `origin_category`, present
on randomly removed words, naming the class the candidate would have belonged
to; and `pos_tags`, the Universal POS tag of each token.

All three are stored verbatim. `pos_tags` is load-bearing rather than
decorative: the service has one class for content-word trigrams while the
calibration study split those into three groups by which parts of speech
actually occur, so the tags are what settles the group. Keeping the raw
values also means a later recalibration can be driven from the service's own
analysis rather than from our reading of it, and a category we do not map yet
is visible in the admin instead of silently falling through to POS tagging.

NULL for local-analyzer picks and for everything ingested before the service
reported each field.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_rename_analysisfailurelog_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='word',
            name='muni_category',
            field=models.CharField(blank=True, editable=False, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name='word',
            name='muni_origin_category',
            field=models.CharField(blank=True, editable=False, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name='word',
            name='muni_pos_tags',
            field=models.CharField(blank=True, editable=False, max_length=120, null=True),
        ),
    ]
