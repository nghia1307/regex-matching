"""
Job persistence.

A job row is the durable record of one natural-language request: what was asked,
what regex the LLM produced, how far the Spark job got, where the output landed,
and how to page through it. Progress *also* streams through Redis (see
:mod:`apps.jobs.services`) because a percentage that changes every second should
not become a write per second against Postgres -- but Postgres stays the source
of truth, so a Redis flush loses nothing but sub-second resolution.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from apps.llm.spec import Operation


class JobStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"
    # Not in the brief's list, but graceful cancellation needs a terminal state
    # that is not a failure.
    CANCELLED = "CANCELLED", "Cancelled"

    @classmethod
    def terminal(cls) -> tuple[str, ...]:
        return (cls.SUCCESS, cls.FAILED, cls.CANCELLED)


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- lifecycle --------------------------------------------------------- #
    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.QUEUED, db_index=True
    )
    progress = models.PositiveSmallIntegerField(default=0)
    phase = models.CharField(max_length=64, default="queued")
    celery_task_id = models.CharField(max_length=64, blank=True, default="")
    attempt = models.PositiveSmallIntegerField(default=0)

    # --- request ----------------------------------------------------------- #
    source_key = models.CharField(max_length=1024)
    sheet_name = models.CharField(max_length=255, blank=True, default="")
    operation = models.CharField(
        max_length=16,
        choices=[(op, Operation.LABELS[op]) for op in Operation.ALL],
        default=Operation.REPLACE,
    )
    natural_language = models.TextField()
    replacement_value = models.CharField(max_length=1024, blank=True, default="")
    target_columns = models.JSONField(default=list)
    force_refresh = models.BooleanField(default=False)

    # --- what the LLM produced --------------------------------------------- #
    regex_pattern = models.TextField(blank=True, default="")
    regex_case_insensitive = models.BooleanField(default=False)
    replacement_template = models.CharField(max_length=512, blank=True, default="")
    extract_group = models.PositiveSmallIntegerField(default=0)
    llm_provider = models.CharField(max_length=32, blank=True, default="")
    llm_model = models.CharField(max_length=64, blank=True, default="")
    llm_explanation = models.TextField(blank=True, default="")
    llm_confidence = models.FloatField(default=0.0)
    llm_cached = models.BooleanField(default=False)
    llm_warnings = models.JSONField(default=list)
    self_test_passed = models.BooleanField(null=True, blank=True)

    # --- result ------------------------------------------------------------ #
    output_path = models.CharField(max_length=1024, blank=True, default="")
    result_columns = models.JSONField(default=list)
    #: [{path, rows, start}] -- lets the API map a page number to one Parquet file
    page_index = models.JSONField(default=list)
    total_rows = models.BigIntegerField(default=0)
    matched_cells = models.BigIntegerField(default=0)
    added_columns = models.JSONField(default=list)

    # --- failure ----------------------------------------------------------- #
    error_message = models.TextField(blank=True, default="")
    error_type = models.CharField(max_length=64, blank=True, default="")

    # --- timing ------------------------------------------------------------ #
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("status", "-created_at")),
            models.Index(fields=("source_key",)),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug convenience
        return f"{self.operation} {self.source_key} [{self.status}]"

    # --- helpers ----------------------------------------------------------- #
    @property
    def is_terminal(self) -> bool:
        return self.status in JobStatus.terminal()

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.finished_at or timezone.now()
        return round((end - self.started_at).total_seconds(), 2)

    @property
    def queue_wait_seconds(self) -> float | None:
        if not self.started_at:
            return None
        return round((self.started_at - self.created_at).total_seconds(), 2)

    def page_count(self, page_size: int) -> int:
        if page_size <= 0 or self.total_rows <= 0:
            return 0
        return (self.total_rows + page_size - 1) // page_size
