"""
Celery application.

Two queues, because the two kinds of work have very different profiles:

* ``spark``  -- long-running distributed jobs. The worker that consumes this
  queue is the Spark *driver*, so it must run single-process (one SparkContext
  per JVM) with fixed driver ports that executors can dial back into.
* ``default`` -- short bookkeeping tasks (file inspection, cleanup) that must
  not queue behind a 5-million-row transformation.
"""
from __future__ import annotations

import os

from celery import Celery
from celery.signals import setup_logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

celery_app = Celery("regexapp")
celery_app.config_from_object("django.conf:settings", namespace="CELERY")

celery_app.conf.update(
    task_default_queue="default",
    task_routes={
        "jobs.run_transformation": {"queue": "spark"},
        "jobs.reap_stale_jobs": {"queue": "default"},
    },
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=24 * 3600,
    # A Spark job holds its worker for minutes; late acks plus
    # reject_on_worker_lost mean a killed worker re-queues rather than silently
    # dropping the job.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    # Hard ceiling so a pathological regex cannot pin a worker forever.
    task_time_limit=3 * 3600,
    task_soft_time_limit=3 * 3600 - 300,
    beat_schedule={
        "reap-stale-jobs": {
            "task": "jobs.reap_stale_jobs",
            "schedule": 300.0,
        }
    },
)

celery_app.autodiscover_tasks(["apps.jobs"])


@setup_logging.connect
def _configure_logging(**_kwargs):
    """Let Django's LOGGING own the format instead of Celery's own handler."""
    from logging.config import dictConfig

    from django.conf import settings

    dictConfig(settings.LOGGING)
