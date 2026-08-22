"""
The Celery task layer, end to end.

Storage is redirected to the local filesystem so the whole pipeline -- read,
resolve, transform, write, index, count -- runs with no MinIO and no cluster,
while still exercising the real code path the container uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.jobs.models import Job, JobStatus
from apps.jobs.tasks import reap_stale_jobs, run_transformation
from apps.llm.spec import Operation

pytestmark = pytest.mark.django_db


@pytest.fixture
def local_storage(monkeypatch, workdir: Path):
    """Point the result store at a temp directory instead of S3."""
    out = workdir / "results"
    out.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "apps.jobs.tasks.result_store.result_uri",
        lambda job_id: f"file://{out}/{job_id}",
    )
    monkeypatch.setattr(
        "apps.jobs.tasks.result_store.pyarrow_prefix",
        lambda job_id: str(out / str(job_id)),
    )
    return out


@pytest.fixture
def job_factory(customers_csv: Path):
    def make(**overrides) -> Job:
        defaults = dict(
            source_key=f"file://{customers_csv}",
            operation=Operation.REPLACE,
            natural_language="find email addresses",
            replacement_value="REDACTED",
            target_columns=["Email"],
        )
        defaults.update(overrides)
        return Job.objects.create(**defaults)

    return make


def run(job: Job) -> Job:
    run_transformation.apply(args=[str(job.id)]).get()
    job.refresh_from_db()
    return job


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_replace_job_succeeds_and_records_everything(spark, local_storage, job_factory):
    job = run(job_factory())

    assert job.status == JobStatus.SUCCESS
    assert job.progress == 100
    assert job.total_rows == 5
    assert job.matched_cells == 3
    assert "@" in job.regex_pattern
    assert job.llm_provider == "fake"
    assert job.result_columns == ["ID", "Name", "Email", "Notes"]
    assert job.page_index, "a page index is required to serve results"
    assert job.started_at and job.finished_at
    assert job.duration_seconds is not None


def test_result_rows_are_readable_through_the_page_index(spark, local_storage, job_factory):
    from apps.sparkeng.results import ChunkRef, read_page

    job = run(job_factory())
    rows = read_page([ChunkRef(**entry) for entry in job.page_index], 1, 10)

    emails = {row["Email"] for row in rows}
    assert "REDACTED" in emails
    assert "not-an-email" in emails
    assert len(rows) == 5


def test_second_identical_job_reuses_the_cached_pattern(spark, local_storage, job_factory):
    from django.core.cache import cache

    cache.clear()
    first = run(job_factory())
    second = run(job_factory())

    assert first.llm_cached is False
    assert second.llm_cached is True
    assert first.regex_pattern == second.regex_pattern


def test_extract_job_adds_a_column(spark, local_storage, job_factory):
    job = run(
        job_factory(
            operation=Operation.EXTRACT,
            natural_language="extract the domain from the email address",
            replacement_value="",
        )
    )
    assert job.status == JobStatus.SUCCESS
    assert job.added_columns == ["Email_extracted"]
    assert "Email_extracted" in job.result_columns


def test_validate_job_flags_bad_rows(spark, local_storage, job_factory):
    job = run(
        job_factory(
            operation=Operation.VALIDATE,
            natural_language="flag invalid email addresses",
            replacement_value="",
        )
    )
    assert job.status == JobStatus.SUCCESS
    assert job.added_columns == ["Email_valid"]
    # rows 4 (empty) and 5 (malformed) fail validation
    assert job.matched_cells == 2


# --------------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------------- #
def test_unknown_column_fails_the_job_with_a_useful_message(spark, local_storage, job_factory):
    job = run(job_factory(target_columns=["NoSuchColumn"]))

    assert job.status == JobStatus.FAILED
    assert job.error_type == "TransformError"
    assert "not in the file" in job.error_message


def test_undescribable_pattern_fails_cleanly(spark, local_storage, job_factory):
    job = run(job_factory(natural_language="please intuit what I mean"))

    assert job.status == JobStatus.FAILED
    assert job.error_type == "RegexResolutionError"


def test_missing_source_file_fails_without_a_traceback_leak(spark, local_storage, job_factory):
    job = job_factory(source_key="file:///nonexistent/nope.csv")
    with pytest.raises(Exception):
        run_transformation.apply(args=[str(job.id)]).get()
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_message


def test_a_job_cancelled_before_it_starts_never_runs(spark, local_storage, job_factory):
    from apps.jobs import services

    job = job_factory()
    services.request_cancel(job)
    job.refresh_from_db()
    # QUEUED + cancel is resolved immediately by the service layer.
    assert job.status == JobStatus.CANCELLED

    run_transformation.apply(args=[str(job.id)]).get()
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED
    assert job.total_rows == 0


def test_spark_cancellation_is_recorded_as_cancelled_not_failed(
    spark, local_storage, job_factory, monkeypatch
):
    """
    Cancelling a Spark job group makes the in-flight action raise inside the
    driver -- it does not come back through our own checkpoint. That exception
    has to be translated, or a cancellation the user asked for is reported as a
    crash with a Java stack trace attached.
    """
    from apps.jobs import services

    job = job_factory()

    def explode_like_spark(*_args, **_kwargs):
        # Stand-in for the Py4JJavaError Spark raises on job-group cancellation.
        raise RuntimeError("Job 30 cancelled part of cancelled job group")

    monkeypatch.setattr("apps.jobs.tasks.result_store.write_result", explode_like_spark)
    # The flag is what a POST /cancel/ sets; the driver observes it.
    services.request_cancel(job)
    Job.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING)

    run_transformation.apply(args=[str(job.id)]).get()
    job.refresh_from_db()

    assert job.status == JobStatus.CANCELLED
    assert "Traceback" not in job.error_message


def test_a_genuine_crash_during_the_write_still_fails(spark, local_storage, job_factory):
    """The cancellation translation must not swallow real errors."""
    import pytest as _pytest

    job = job_factory()
    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "apps.jobs.tasks.result_store.write_result",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")),
        )
        with _pytest.raises(RuntimeError, match="disk on fire"):
            run_transformation.apply(args=[str(job.id)]).get()

    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert "disk on fire" in job.error_message


def test_running_a_missing_job_id_does_not_explode():
    import uuid

    result = run_transformation.apply(args=[str(uuid.uuid4())]).get()
    assert result["status"] == "MISSING"


def test_reaper_fails_jobs_whose_worker_died(job_factory):
    from datetime import timedelta

    from django.utils import timezone

    job = job_factory()
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.RUNNING, started_at=timezone.now() - timedelta(hours=9)
    )

    assert reap_stale_jobs(max_age_hours=6) == 1
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_type == "StaleJob"


def test_reaper_leaves_healthy_jobs_alone(job_factory):
    from django.utils import timezone

    job = job_factory()
    Job.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING, started_at=timezone.now())
    assert reap_stale_jobs(max_age_hours=6) == 0
