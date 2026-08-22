"""
API contract tests.

The headline requirement is that submit returns immediately with a job id, so
that is asserted directly: the endpoint must not run the pipeline, and the
dispatch must happen after the transaction commits.
"""
from __future__ import annotations

import uuid

import pytest
from django.urls import reverse

from apps.jobs.models import Job, JobStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture(autouse=True)
def _stub_storage(monkeypatch):
    """No network in API tests: stub the S3 control-plane calls."""
    monkeypatch.setattr("apps.storage.s3.head_object", lambda key, bucket=None: {"ContentLength": 10})
    monkeypatch.setattr(
        "apps.storage.s3.list_files",
        lambda prefix=None, bucket=None, extensions=None: [],
    )


@pytest.fixture
def payload():
    return {
        "source_key": "raw/customers.csv",
        "operation": "REPLACE",
        "natural_language": "Find email addresses",
        "replacement_value": "REDACTED",
        "target_columns": ["Email"],
    }


# --------------------------------------------------------------------------- #
# submit
# --------------------------------------------------------------------------- #
def test_submit_returns_202_with_a_job_id_immediately(client, payload, monkeypatch):
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        "apps.jobs.tasks.run_transformation.apply_async",
        lambda *args, **kwargs: type("R", (), {"id": kwargs.get("task_id", "x")})(),
    )
    monkeypatch.setattr(
        "apps.jobs.services.transaction.on_commit",
        lambda func: dispatched.append(func),
    )

    response = client.post(reverse("job-list"), payload, format="json")

    assert response.status_code == 202
    assert response["Location"].startswith("/api/jobs/")
    body = response.json()
    assert body["status"] == JobStatus.QUEUED
    assert uuid.UUID(body["id"])
    assert body["regex"]["pattern"] == "", "no pattern yet -- that is the worker's job"
    assert len(dispatched) == 1, "work must be handed to Celery, not done inline"


def test_dispatch_waits_for_the_transaction_to_commit(client, payload, monkeypatch, django_capture_on_commit_callbacks):
    calls: list[str] = []

    class FakeResult:
        id = "task-1"

    def fake_apply_async(*args, **kwargs):
        calls.append(kwargs.get("task_id", ""))
        return FakeResult()

    monkeypatch.setattr("apps.jobs.tasks.run_transformation.apply_async", fake_apply_async)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("job-list"), payload, format="json")

    assert response.status_code == 202
    assert calls == [response.json()["id"]], "task id should equal the job id"


@pytest.mark.parametrize(
    "mutation,field",
    [
        ({"target_columns": []}, "target_columns"),
        ({"natural_language": "hi"}, "natural_language"),
        ({"source_key": "raw/notes.txt"}, "source_key"),
        ({"replacement_value": ""}, "replacement_value"),
        ({"operation": "DROP_TABLE"}, "operation"),
    ],
)
def test_invalid_submissions_are_rejected_before_queueing(client, payload, mutation, field):
    payload.update(mutation)
    response = client.post(reverse("job-list"), payload, format="json")

    assert response.status_code == 400
    assert field in str(response.json())
    assert Job.objects.count() == 0


def test_non_replace_operations_do_not_need_a_replacement_value(client, payload, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.transaction.on_commit", lambda func: None)
    payload.update({"operation": "VALIDATE", "replacement_value": ""})
    response = client.post(reverse("job-list"), payload, format="json")
    assert response.status_code == 202


# --------------------------------------------------------------------------- #
# poll / result
# --------------------------------------------------------------------------- #
def test_job_detail_reports_live_progress_from_redis(client):
    from apps.jobs import services

    job = Job.objects.create(
        source_key="raw/x.csv",
        natural_language="find emails",
        target_columns=["Email"],
        status=JobStatus.RUNNING,
        progress=5,
        phase="starting",
    )
    services.set_progress(str(job.id), 61, "transforming")

    body = client.get(reverse("job-detail", args=[job.id])).json()
    assert body["progress"] == 61
    assert body["phase"] == "transforming"


def test_result_is_409_until_the_job_succeeds(client):
    job = Job.objects.create(
        source_key="raw/x.csv",
        natural_language="find emails",
        target_columns=["Email"],
        status=JobStatus.RUNNING,
    )
    response = client.get(reverse("job-result", args=[job.id]))
    assert response.status_code == 409
    assert response.json()["error"]["detail"]["status"] == JobStatus.RUNNING


def test_unknown_job_is_404(client):
    response = client.get(reverse("job-detail", args=[uuid.uuid4()]))
    assert response.status_code == 404


def test_page_size_is_capped(client, settings):
    settings.API_MAX_PAGE_SIZE = 100
    job = Job.objects.create(
        source_key="raw/x.csv",
        natural_language="find emails",
        target_columns=["Email"],
        status=JobStatus.SUCCESS,
        total_rows=10,
        result_columns=["ID"],
        page_index=[],
    )
    body = client.get(
        reverse("job-result", args=[job.id]), {"page": 1, "page_size": 99999}
    ).json()
    assert body["page_size"] == 100


def test_cancel_on_a_finished_job_is_409(client):
    job = Job.objects.create(
        source_key="raw/x.csv",
        natural_language="find emails",
        target_columns=["Email"],
        status=JobStatus.SUCCESS,
    )
    response = client.post(reverse("job-cancel", args=[job.id]))
    assert response.status_code == 409


def test_cancel_marks_a_queued_job_cancelled(client):
    job = Job.objects.create(
        source_key="raw/x.csv",
        natural_language="find emails",
        target_columns=["Email"],
        status=JobStatus.QUEUED,
    )
    response = client.post(reverse("job-cancel", args=[job.id]))
    assert response.status_code == 202
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED


# --------------------------------------------------------------------------- #
# supporting endpoints
# --------------------------------------------------------------------------- #
def test_operations_endpoint_describes_the_vocabulary(client):
    operations = client.get(reverse("operations")).json()["operations"]
    values = {item["value"] for item in operations}
    assert values == {"REPLACE", "MASK", "EXTRACT", "VALIDATE"}
    assert next(o for o in operations if o["value"] == "REPLACE")["needs_replacement"] is True


def test_preview_requires_a_key(client):
    response = client.get(reverse("file-preview"))
    assert response.status_code == 400


def test_metrics_endpoint_reports_job_counts(client):
    Job.objects.create(
        source_key="raw/x.csv",
        natural_language="find emails",
        target_columns=["Email"],
        status=JobStatus.SUCCESS,
        total_rows=1000,
        matched_cells=12,
    )
    body = client.get(reverse("metrics")).json()
    assert body["jobs"]["jobs_total"] == 1
    assert body["jobs"]["rows_processed_total"] == 1000
    assert "llm" in body and "workers" in body


def test_health_reports_each_dependency(client):
    body = client.get(reverse("health")).json()
    assert set(body["checks"]) >= {"database", "redis", "object_storage", "llm", "spark"}
