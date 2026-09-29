from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_string_field_cleanup"),
    ]

    operations = [
        migrations.AddField(
            model_name="word",
            name="expected_embedding",
            field=models.BinaryField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="testedparagraph",
            name="computed_score",
            field=models.FloatField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="testedparagraph",
            name="computed_grade",
            field=models.CharField(blank=True, default="", editable=False, max_length=20),
        ),
    ]
