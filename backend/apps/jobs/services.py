"""
Job service layer.

Sits between the API and the task layer so neither one has to know how the other
works: views never import Celery tasks' internals, and tasks never build HTTP
responses. It also owns the two-tier progress store.

**Why two tiers.** Progress ticks roughly once a second for the life of a job. If
each tick were a Postgres UPDATE, a handful of concurrent jobs would generate
constant write traffic and row churn for data nobody keeps. So ticks go to Redis
(volatile, cheap, expires by itself) and Postgres is written only on state
*transitions*. Reads prefer Redis and fall back to the row, so losing Redis costs
sub-second resolution and nothing else.
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.llm.spec import Operation, RegexSpec

from .models import Job, JobStatus

logger = logging.getLogger(__name__)

PROGRESS_TTL_SECONDS = 6 * 3600
CANCEL_TTL_SECONDS = 24 * 3600


# --------------------------------------------------------------------------- #
# cache keys
# --------------------------------------------------------------------------- #
def progress_key(job_id: str) -> str:
    return f"job:{job_id}:progress"


def cancel_key(job_id: str) -> str:
    return f"job:{job_id}:cancel"


# --------------------------------------------------------------------------- #
# submission
# --------------------------------------------------------------------------- #
def submit_job(validated: dict[str, Any]) -> Job:
    """
    Persist a QUEUED job and hand it to Celery.

    The dispatch happens in ``on_commit`` so a worker can never pick up a job id
    that has not been committed yet -- a classic race that shows up as a random
    ``Job.DoesNotExist`` under load.
    """
    from .tasks import run_transformation

    job = Job.objects.create(
        source_key=validated["source_key"],
        sheet_name=validated.get("sheet_name", "") or "",
        operation=validated.get("operation", Operation.REPLACE),
        natural_language=validated["natural_language"],
        replacement_value=validated.get("replacement_value", "") or "",
        target_columns=validated["target_columns"],
        force_refresh=validated.get("force_refresh", False),
    )
    set_progress(str(job.id), 0, "queued")

    def _dispatch() -> None:
        result = run_transformation.apply_async(args=[str(job.id)], task_id=str(job.id))
        Job.objects.filter(pk=job.pk).update(celery_task_id=result.id)

    transaction.on_commit(_dispatch)
    logger.info("job queued id=%s op=%s", job.id, job.operation)
    return job


# --------------------------------------------------------------------------- #
# progress
# --------------------------------------------------------------------------- #
def set_progress(job_id: str, percent: int, phase: str) -> None:
    cache.set(
        progress_key(job_id),
        {"progress": int(percent), "phase": phase},
        timeout=PROGRESS_TTL_SECONDS,
    )


def get_progress(job: Job) -> tuple[int, str]:
    """Live progress if Redis has it, otherwise the last persisted value."""
    if job.is_terminal:
        return job.progress, job.phase
    cached = cache.get(progress_key(str(job.id)))
    if isinstance(cached, dict):
        return int(cached.get("progress", job.progress)), str(
            cached.get("phase", job.phase)
        )
    return job.progress, job.phase


# --------------------------------------------------------------------------- #
# state transitions (persisted)
# --------------------------------------------------------------------------- #
def mark_running(job_id: str, attempt: int = 1) -> None:
    Job.objects.filter(pk=job_id).update(
        status=JobStatus.RUNNING,
        started_at=timezone.now(),
        phase="starting",
        progress=1,
        attempt=attempt,
        error_message="",
        error_type="",
    )
    set_progress(job_id, 1, "starting")


def record_regex(job_id: str, spec: RegexSpec, cached: bool) -> None:
    Job.objects.filter(pk=job_id).update(
        regex_pattern=spec.pattern,
        regex_case_insensitive=spec.case_insensitive,
        replacement_template=spec.replacement_template,
        extract_group=spec.group,
        llm_provider=spec.provider,
        llm_model=spec.model,
        llm_explanation=spec.explanation,
        llm_confidence=spec.confidence,
        llm_cached=cached,
        llm_warnings=spec.warnings,
        self_test_passed=spec.self_test_passed,
    )


def mark_success(job_id: str, **fields: Any) -> None:
    Job.objects.filter(pk=job_id).update(
        status=JobStatus.SUCCESS,
        progress=100,
        phase="done",
        finished_at=timezone.now(),
        **fields,
    )
    set_progress(job_id, 100, "done")


def mark_failed(job_id: str, error: BaseException | str, error_type: str = "") -> None:
    message = str(error)
    kind = error_type or type(error).__name__ if not isinstance(error, str) else error_type
    Job.objects.filter(pk=job_id).update(
        status=JobStatus.FAILED,
        phase="failed",
        finished_at=timezone.now(),
        error_message=message[:4000],
        error_type=(kind or "Error")[:64],
    )
    logger.error("job %s failed: %s", job_id, message)


def mark_cancelled(job_id: str) -> None:
    Job.objects.filter(pk=job_id).update(
        status=JobStatus.CANCELLED,
        phase="cancelled",
        finished_at=timezone.now(),
    )
    set_progress(job_id, 0, "cancelled")


# --------------------------------------------------------------------------- #
# cancellation
# --------------------------------------------------------------------------- #
def request_cancel(job: Job) -> bool:
    """
    Ask a job to stop. Returns False if it had already finished.

    Two mechanisms, because they cover different states:

    * the Redis flag is polled by the driver's progress thread, which calls
      ``cancelJobGroup`` -- this is what stops work already running on executors;
    * ``AsyncResult.revoke`` stops a job that is still sitting in the queue.
    """
    if job.is_terminal:
        return False

    cache.set(cancel_key(str(job.id)), 1, timeout=CANCEL_TTL_SECONDS)

    try:
        from config.celery import celery_app

        celery_app.control.revoke(job.celery_task_id or str(job.id), terminate=False)
    except Exception:  # noqa: BLE001 - broker may be unreachable; flag still set
        logger.warning("revoke failed for job %s", job.id, exc_info=True)

    if job.status == JobStatus.QUEUED:
        # Nothing has started, so nothing will observe the flag.
        mark_cancelled(str(job.id))
    return True


def is_cancel_requested(job_id: str) -> bool:
    return bool(cache.get(cancel_key(job_id)))


def clear_cancel(job_id: str) -> None:
    cache.delete(cancel_key(job_id))


# --------------------------------------------------------------------------- #
# observability
# --------------------------------------------------------------------------- #
def job_metrics() -> dict[str, Any]:
    """Counts and timings for /api/metrics -- one grouped query, no scans."""
    from django.db.models import Avg, Count, Max, Sum

    by_status = {
        row["status"]: row["n"]
        for row in Job.objects.values("status").annotate(n=Count("id"))
    }
    finished = Job.objects.filter(status=JobStatus.SUCCESS)
    aggregates = finished.aggregate(
        rows=Sum("total_rows"),
        matched=Sum("matched_cells"),
        biggest=Max("total_rows"),
        avg_confidence=Avg("llm_confidence"),
    )
    return {
        "jobs_by_status": {status: by_status.get(status, 0) for status, _ in JobStatus.choices},
        "jobs_total": sum(by_status.values()),
        "rows_processed_total": int(aggregates["rows"] or 0),
        "cells_matched_total": int(aggregates["matched"] or 0),
        "largest_job_rows": int(aggregates["biggest"] or 0),
        "avg_llm_confidence": round(aggregates["avg_confidence"] or 0.0, 3),
        "cache_hit_jobs": Job.objects.filter(llm_cached=True).count(),
    }


def page_size_or_default(requested: Any, default: int = 50) -> int:
    try:
        size = int(requested)
    except (TypeError, ValueError):
        return default
    return max(1, min(size, settings.API_MAX_PAGE_SIZE))
