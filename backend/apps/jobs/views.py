"""
HTTP layer.

Three rules hold everywhere in this module:

* **Submit never blocks.** ``POST /api/jobs/`` validates, writes a row, queues a
  Celery task and returns ``202`` with a job id. It does no S3 reads beyond a
  HEAD, no LLM call and no Spark work.
* **Reads are bounded.** File previews read a capped byte range; result pages
  read one Parquet row group. No endpoint's cost scales with the size of the
  data.
* **Errors are shaped.** Everything funnels through
  :func:`apps.jobs.exceptions.api_exception_handler`.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.llm.service import provider_health
from apps.llm.spec import Operation
from apps.sparkeng import results as result_store
from apps.sparkeng.session import cluster_status
from apps.storage import s3

from . import services
from .models import Job, JobStatus
from .serializers import (
    JobCreateSerializer,
    JobSerializer,
    JobSummarySerializer,
    PageQuerySerializer,
)

logger = logging.getLogger(__name__)


class HealthView(APIView):
    """Dependency-by-dependency health, so a broken stack is obvious at a glance."""

    def get(self, request):
        checks: dict[str, object] = {}

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            checks["database"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"ok": False, "error": str(exc)}

        try:
            from django.core.cache import cache

            cache.set("health:ping", "pong", timeout=10)
            checks["redis"] = {"ok": cache.get("health:ping") == "pong"}
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = {"ok": False, "error": str(exc)}

        try:
            files = s3.list_files()
            checks["object_storage"] = {
                "ok": True,
                "bucket": settings.S3_BUCKET,
                "endpoint": settings.S3_ENDPOINT_URL or "aws",
                "input_files": len(files),
            }
        except Exception as exc:  # noqa: BLE001
            checks["object_storage"] = {"ok": False, "error": str(exc)}

        checks["llm"] = provider_health()
        checks["spark"] = cluster_status()

        healthy = all(
            not isinstance(value, dict) or value.get("ok", True)
            for value in checks.values()
        )
        return Response(
            {"status": "ok" if healthy else "degraded", "checks": checks},
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class MetricsView(APIView):
    """Task and worker metrics. Flower has the detail; this is the summary."""

    def get(self, request):
        payload = {
            "jobs": services.job_metrics(),
            "llm": provider_health(),
            "workers": _worker_stats(),
            "config": {
                "spark_master": settings.SPARK_MASTER_URL,
                "shuffle_partitions": settings.SPARK_SHUFFLE_PARTITIONS,
                "max_records_per_file": settings.SPARK_MAX_RECORDS_PER_FILE,
                "max_page_size": settings.API_MAX_PAGE_SIZE,
            },
        }
        return Response(payload)


def _worker_stats() -> dict:
    """Ask the broker who is alive. Short timeout: this is a status page."""
    try:
        from config.celery import celery_app

        inspector = celery_app.control.inspect(timeout=1.0)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        return {
            "online": sorted(active.keys()),
            "active_tasks": {name: len(tasks) for name, tasks in active.items()},
            "reserved_tasks": {name: len(tasks) for name, tasks in reserved.items()},
        }
    except Exception as exc:  # noqa: BLE001
        return {"online": [], "error": str(exc)}


class OperationListView(APIView):
    """The operation vocabulary, so the UI is not hard-coded to it."""

    def get(self, request):
        return Response(
            {
                "operations": [
                    {
                        "value": operation,
                        "label": Operation.LABELS[operation],
                        "needs_replacement": operation in Operation.NEEDS_REPLACEMENT,
                        "creates_column": operation in Operation.CREATES_COLUMN,
                    }
                    for operation in Operation.ALL
                ]
            }
        )


class FileListView(APIView):
    """Browse selectable files in the bucket."""

    def get(self, request):
        prefix = request.query_params.get("prefix")
        files = s3.list_files(prefix=prefix)
        return Response(
            {
                "bucket": settings.S3_BUCKET,
                "prefix": prefix if prefix is not None else settings.S3_RAW_PREFIX,
                "count": len(files),
                "files": [item.as_dict() for item in files],
            }
        )


class FilePreviewView(APIView):
    """
    Header + a few rows, so the user can pick target columns.

    Bounded by design: CSV/TSV is a ranged read of the first 256 KB, Excel is
    capped by ``EXCEL_MAX_BYTES``. Previewing a 5 GB file costs the same as
    previewing a 5 KB one.
    """

    def get(self, request):
        key = request.query_params.get("key", "").strip()
        if not key:
            return Response(
                {"error": {"type": "ValidationError", "message": "key is required"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        sheet = request.query_params.get("sheet") or None
        rows = min(int(request.query_params.get("rows", 5) or 5), 20)

        from django.core.cache import cache

        cache_key = f"preview:{key}:{sheet or ''}:{rows}"
        cached = cache.get(cache_key)
        if cached:
            return Response({**cached, "cached": True})

        preview = s3.preview_file(key, rows=rows, sheet=sheet).as_dict()
        cache.set(cache_key, preview, timeout=3600)
        return Response({**preview, "cached": False})


class JobListCreateView(ListAPIView):
    """``GET`` recent jobs, ``POST`` a new one."""

    serializer_class = JobSummarySerializer

    def get_queryset(self):
        queryset = Job.objects.all()
        job_status = self.request.query_params.get("status")
        if job_status:
            queryset = queryset.filter(status=job_status.upper())
        return queryset

    def post(self, request):
        serializer = JobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job = services.submit_job(serializer.validated_data)
        # 202: accepted, not done. The client polls the returned id.
        return Response(
            JobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED,
            headers={"Location": f"/api/jobs/{job.id}/"},
        )


class JobDetailView(APIView):
    """Poll target: status, progress, phase, the generated regex, and errors."""

    def get(self, request, job_id):
        job = _get_job(job_id)
        return Response(JobSerializer(job).data)

    def delete(self, request, job_id):
        """Delete a job row and its Parquet output."""
        from .tasks import delete_result

        job = _get_job(job_id)
        if not job.is_terminal:
            services.request_cancel(job)
        delete_result.delay(str(job.id))
        job.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class JobCancelView(APIView):
    def post(self, request, job_id):
        job = _get_job(job_id)
        accepted = services.request_cancel(job)
        if not accepted:
            return Response(
                {
                    "error": {
                        "type": "Conflict",
                        "message": f"job already finished with status {job.status}",
                    }
                },
                status=status.HTTP_409_CONFLICT,
            )
        job.refresh_from_db()
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class JobResultView(APIView):
    """
    One page of the processed data.

    Reads straight from the job's Parquet output with pyarrow -- no Spark, no
    Celery, no full-result materialisation. Cost tracks ``page_size``.
    """

    def get(self, request, job_id):
        job = _get_job(job_id)

        if job.status != JobStatus.SUCCESS:
            return Response(
                {
                    "error": {
                        "type": "Conflict",
                        "message": f"job is {job.status}; results are not available yet",
                        "detail": {
                            "status": job.status,
                            "progress": services.get_progress(job)[0],
                        },
                    }
                },
                status=status.HTTP_409_CONFLICT,
            )

        params = PageQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        page = params.validated_data["page"]
        page_size = params.validated_data["page_size"]

        chunks = [result_store.ChunkRef(**entry) for entry in job.page_index]
        rows = result_store.read_page(chunks, page, page_size, columns=job.result_columns)
        total_pages = job.page_count(page_size)
        offset = (page - 1) * page_size

        return Response(
            {
                "job_id": str(job.id),
                "columns": job.result_columns,
                "added_columns": job.added_columns,
                "rows": rows,
                "page": page,
                "page_size": page_size,
                "total_rows": job.total_rows,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1,
                "row_offset": offset,
                "matched_cells": job.matched_cells,
                "regex": job.regex_pattern,
                "operation": job.operation,
            }
        )


def _get_job(job_id) -> Job:
    from django.http import Http404

    try:
        return Job.objects.get(pk=job_id)
    except (Job.DoesNotExist, ValueError, TypeError) as exc:
        raise Http404(f"job {job_id} not found") from exc
