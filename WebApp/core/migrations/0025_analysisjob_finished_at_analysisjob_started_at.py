from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_alter_languageproficiency_language_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysisjob',
            name='finished_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='analysisjob',
            name='started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
