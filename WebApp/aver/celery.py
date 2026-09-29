import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aver.settings")
app = Celery("aver")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.conf.broker_connection_retry_on_startup = True
