from django.db import migrations


def semicorrect_to_incorrect(apps, schema_editor):
    TestedParagraph = apps.get_model("core", "TestedParagraph")
    TestedParagraph.objects.filter(computed_grade="semicorrect").update(
        computed_grade="incorrect"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_scoring_fields"),
    ]

    operations = [
        migrations.RunPython(
            semicorrect_to_incorrect, reverse_code=migrations.RunPython.noop
        ),
    ]
