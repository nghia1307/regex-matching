"""
Shared test fixtures.

The Spark fixture is session-scoped and runs ``local[2]``: real multi-partition
execution, real Catalyst, real Parquet output -- just in-process. That is what
lets the task/Spark layer be tested without MinIO, Redis, a cluster or a Gemini
key.
"""
from __future__ import annotations

import csv
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.fake_llm import FakeLLMProvider


@pytest.fixture(autouse=True)
def _fake_llm_provider(monkeypatch):
    """
    Route every regex-resolution call to the offline :class:`FakeLLMProvider`.

    Production always requires a real Gemini key; this fixture is what keeps
    the test suite hermetic without one. Tests that specifically exercise
    Gemini/fallback/error behaviour monkeypatch ``build_provider`` themselves,
    which simply overrides this default within that test.
    """
    monkeypatch.setattr("apps.llm.service.build_provider", lambda name="": FakeLLMProvider())


@pytest.fixture(scope="session")
def spark():
    from apps.sparkeng.session import get_spark, stop_spark

    session = get_spark("regexapp-tests")
    yield session
    stop_spark()


@pytest.fixture
def workdir():
    path = Path(tempfile.mkdtemp(prefix="regexapp-case-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def customers_csv(workdir: Path) -> Path:
    """The worked example from the brief, plus null and near-miss rows."""
    path = workdir / "customers.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["ID", "Name", "Email", "Notes"])
        writer.writerow(["1", "John Doe", "john.doe@example.com", "primary"])
        writer.writerow(["2", "Jane Smith", "jane_smith@domain.com", "cc bob@x.io"])
        writer.writerow(["3", "Alice Brown", "alice.brown@website.org", "none"])
        writer.writerow(["4", "No Email", "", "missing"])
        writer.writerow(["5", "Bad Email", "not-an-email", "invalid"])
    return path


@pytest.fixture
def customers_df(spark, customers_csv: Path):
    from apps.sparkeng.reader import read_source

    return read_source(spark, f"file://{customers_csv}")


@pytest.fixture
def email_spec():
    from apps.llm.spec import Operation, RegexSpec

    return RegexSpec(
        pattern=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b",
        operation=Operation.REPLACE,
        explanation="email addresses",
        confidence=0.9,
        provider="test",
        model="test",
    )
