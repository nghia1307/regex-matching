"""
Celery tasks -- the only place heavy work happens.

``run_transformation`` is the whole pipeline for one job:

    read -> resolve regex (cache or LLM) -> plan -> write Parquet
         -> build page index -> count matches

Each step runs inside a named progress phase, so the UI can say *what* is
happening rather than just showing a moving bar. Nothing in here returns data to
the caller: the API reads the job row and pages the Parquet output, which is what
keeps a five-million-row result from ever passing through a web worker.
"""
from __future__ import annotations

import logging
import sys

from botocore.exceptions import ConnectionError as BotoConnectionError
from botocore.exceptions import EndpointConnectionError
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_process_init
from django.conf import settings

from apps.llm.providers import LLMTransientError
from apps.llm.service import RegexResolutionError, resolve_regex
from apps.llm.spec import RegexRequest
from apps.llm.validation import RegexRejected
from apps.sparkeng import results as result_store
from apps.sparkeng.engine import TransformError, plan_transformation, sample_values, validate_columns
from apps.sparkeng.progress import JobCancelled, ProgressReporter
from apps.sparkeng.reader import read_source
from apps.sparkeng.session import get_spark
from apps.storage import s3

from . import services
from .models import Job, JobStatus

logger = logging.getLogger(__name__)

#: Infrastructure hiccups worth another attempt. Deterministic problems (a bad
#: regex, a missing column) are never retried -- they would fail identically.
RETRYABLE = (
    LLMTransientError,
    EndpointConnectionError,
    BotoConnectionError,
    ConnectionError,
    TimeoutError,
)

MAX_RETRIES = 2


@shared_task(bind=True, name="jobs.run_transformation", max_retries=MAX_RETRIES)
def run_transformation(self, job_id: str) -> dict:
    """Run one natural-language transformation job end to end."""
    try:
        job = Job.objects.get(pk=job_id)
    except Job.DoesNotExist:
        logger.error("job %s vanished before it could run", job_id)
        return {"job_id": job_id, "status": "MISSING"}

    if job.is_terminal:
        logger.info("job %s already %s, nothing to do", job_id, job.status)
        return {"job_id": job_id, "status": job.status}

    if services.is_cancel_requested(job_id):
        services.mark_cancelled(job_id)
        return {"job_id": job_id, "status": JobStatus.CANCELLED}

    attempt = self.request.retries + 1
    services.mark_running(job_id, attempt=attempt)

    try:
        payload = _execute(job)
    except JobCancelled:
        services.mark_cancelled(job_id)
        services.clear_cancel(job_id)
        logger.warning("job %s cancelled", job_id)
        return {"job_id": job_id, "status": JobStatus.CANCELLED}
    except SoftTimeLimitExceeded as exc:
        services.mark_failed(job_id, "job exceeded its time limit", "Timeout")
        raise exc
    except RETRYABLE as exc:
        if self.request.retries < MAX_RETRIES:
            delay = 5 * (2 ** self.request.retries)
            services.set_progress(job_id, 0, f"retrying in {delay}s ({type(exc).__name__})")
            logger.warning("job %s retrying after %s", job_id, exc)
            raise self.retry(exc=exc, countdown=delay)
        services.mark_failed(job_id, exc)
        raise
    except (TransformError, RegexResolutionError, RegexRejected, s3.StorageError) as exc:
        # User-fixable: bad column, unusable description, missing file.
        services.mark_failed(job_id, exc)
        return {"job_id": job_id, "status": JobStatus.FAILED, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - last resort, always recorded
        services.mark_failed(job_id, exc)
        logger.exception("job %s crashed", job_id)
        raise

    services.mark_success(job_id, **payload)
    services.clear_cancel(job_id)
    return {"job_id": job_id, "status": JobStatus.SUCCESS, **{
        key: payload[key] for key in ("total_rows", "matched_cells") if key in payload
    }}


def _execute(job: Job) -> dict:
    """
    Run the pipeline under a progress reporter.

    The try/except is load-bearing: cancelling a Spark job group makes the
    *in-flight action* raise inside the driver, not our own checkpoint. Without
    translating that back into :class:`JobCancelled`, a cancellation the user
    asked for would be recorded as a crash, complete with a Java stack trace.
    """
    job_id = str(job.id)
    spark = get_spark()

    def sink(percent: int, phase: str) -> None:
        services.set_progress(job_id, percent, phase)

    with ProgressReporter(
        spark,
        job_id,
        sink=sink,
        cancel_check=lambda: services.is_cancel_requested(job_id),
    ) as reporter:
        try:
            return _run_phases(job, spark, reporter)
        except JobCancelled:
            raise
        except Exception as exc:
            if reporter.cancelled or services.is_cancel_requested(job_id):
                raise JobCancelled("spark job group was cancelled") from exc
            raise


def _run_phases(job: Job, spark, reporter: ProgressReporter) -> dict:
    """The pipeline itself. Returns the fields to persist on success."""
    job_id = str(job.id)

    # --- 1. source ----------------------------------------------------- #
    with reporter.phase("reading source", 3, 14):
        frame = read_source(spark, job.source_key, sheet=job.sheet_name or None)
        targets = validate_columns(frame, job.target_columns)
        # A tiny, single-partition read: enough sample values to give the
        # model format context without shipping a column anywhere.
        samples = sample_values(frame, targets)
        reporter.raise_if_cancelled()

    # --- 2. natural language -> regex ---------------------------------- #
    with reporter.phase("resolving pattern", 14, 24):
        spec, was_cached = resolve_regex(
            RegexRequest(
                description=job.natural_language,
                operation=job.operation,
                columns=targets,
                sample_values=samples,
                replacement_value=job.replacement_value,
            ),
            force_refresh=job.force_refresh,
        )
        services.record_regex(job_id, spec, was_cached)
        logger.info(
            "job %s pattern=%r cached=%s provider=%s",
            job_id,
            spec.pattern,
            was_cached,
            spec.provider,
        )
        reporter.raise_if_cancelled()

    # --- 3. transform + write ------------------------------------------ #
    output_uri = result_store.result_uri(job_id)
    with reporter.phase("transforming", 24, 78):
        plan = plan_transformation(
            frame, spec, targets, replacement_value=job.replacement_value
        )
        result_store.write_result(plan.dataframe, output_uri)

    # --- 4. page index -------------------------------------------------- #
    with reporter.phase("indexing result", 78, 90):
        chunks, total_rows, columns = result_store.build_page_index(
            result_store.pyarrow_prefix(job_id)
        )

    # --- 5. match count ------------------------------------------------- #
    with reporter.phase("counting matches", 90, 99):
        matched = result_store.count_matches(spark, output_uri)

    if reporter.cancelled:
        raise JobCancelled("cancelled during finalisation")

    return {
        "output_path": output_uri,
        "page_index": [chunk.as_dict() for chunk in chunks],
        "result_columns": columns or plan.result_columns,
        "added_columns": plan.added_columns,
        "total_rows": total_rows,
        "matched_cells": matched,
    }


# --------------------------------------------------------------------------- #
# supporting tasks
# --------------------------------------------------------------------------- #

@shared_task(name="jobs.reap_stale_jobs")
def reap_stale_jobs(max_age_hours: int = 6) -> int:
    """
    Fail jobs whose worker died mid-flight.

    ``task_acks_late`` re-queues most losses, but a worker killed hard (OOM,
    ``docker kill``) can leave a row RUNNING forever. Without this the UI would
    poll such a job until the end of time.
    """
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(hours=max_age_hours)
    stale = Job.objects.filter(status=JobStatus.RUNNING, started_at__lt=cutoff)
    count = 0
    for job in stale:
        services.mark_failed(
            str(job.id),
            f"no progress for over {max_age_hours}h -- the worker probably died",
            "StaleJob",
        )
        count += 1
    if count:
        logger.warning("reaped %s stale job(s)", count)
    return count


@shared_task(name="jobs.delete_result")
def delete_result(job_id: str) -> int:
    """Remove a job's Parquet output from the bucket."""
    return s3.delete_prefix(result_store.result_prefix(job_id) + "/")


# --------------------------------------------------------------------------- #
# worker warm-up
# --------------------------------------------------------------------------- #
@worker_process_init.connect
def _warm_spark(**_kwargs):
    """
    Start the SparkSession in the prefork *child*, as soon as it boots.

    Two reasons this hangs off ``worker_process_init`` rather than
    ``worker_ready``:

    * A JVM must never be inherited across a fork. ``worker_ready`` fires in the
      parent, so warming there would leave every child holding a copy of a py4j
      socket it does not own -- a classic and very confusing hang.
    * Creating a session means starting a JVM and registering with the master:
      ten to twenty seconds. Paying that at boot means the first user request
      does not.

    Only the child that consumes the ``spark`` queue needs this, but a child of
    the light worker warming a session it never uses would waste a JVM, so it is
    gated on the master URL being a real cluster.
    """
    if settings.SPARK_MASTER_URL.startswith("local"):
        return
    if not _is_spark_worker():
        return

    import threading

    def _warm() -> None:
        try:
            spark = get_spark()
            logger.info("spark warmed up: %s", spark.sparkContext.applicationId)
        except Exception:  # noqa: BLE001 - the first job will report properly
            logger.exception("spark warm-up failed")

    threading.Thread(target=_warm, name="spark-warmup", daemon=True).start()


def _is_spark_worker() -> bool:
    """True when this process was started to consume the ``spark`` queue."""
    argv = " ".join(sys.argv)
    return "spark" in argv or "--queues" not in argv
