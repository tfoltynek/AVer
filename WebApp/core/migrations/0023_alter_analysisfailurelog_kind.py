from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_analysisfailurelog'),
    ]

    operations = [
        migrations.AlterField(
            model_name='analysisfailurelog',
            name='kind',
            field=models.CharField(
                choices=[
                    ('network_error', 'Network error'),
                    ('http_status', 'HTTP non-200 status'),
                    ('non_json', 'Non-JSON response body'),
                    ('bad_submit_response', '202 without a job_id'),
                    ('expired', 'Job unknown or expired'),
                    ('job_failed', 'MUNI reported the job failed'),
                    ('deadline_exceeded', 'Job did not finish in time'),
                    ('unhandled', 'Unhandled exception'),
                ],
                max_length=20,
            ),
        ),
    ]
