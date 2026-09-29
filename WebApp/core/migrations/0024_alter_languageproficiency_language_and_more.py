# Pre-existing model drift (verbose_name/choices tweaks on LanguageProficiency
# and User.email) that was never migrated; caught by makemigrations --check
# while adding the AnalysisJob timestamps. No schema-level SQL changes.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_alter_analysisfailurelog_kind'),
    ]

    operations = [
        migrations.AlterField(
            model_name='languageproficiency',
            name='language',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='core.language', verbose_name='Language'),
        ),
        migrations.AlterField(
            model_name='languageproficiency',
            name='proficiency',
            field=models.CharField(choices=[('A1', 'A1 (Beginner)'), ('A2', 'A2 (Elementary)'), ('B1', 'B1 (Intermediate)'), ('B2', 'B2 (Upper Intermediate)'), ('C1', 'C1 (Advanced)'), ('C2', 'C2 (Proficient)'), ('native', 'Native')], max_length=6, verbose_name='Proficiency'),
        ),
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.EmailField(max_length=254, unique=True, verbose_name='E-mail'),
        ),
    ]
