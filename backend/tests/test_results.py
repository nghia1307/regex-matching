"""
Result materialisation and paging.

The contract under test: whatever the partition layout, page N of size S must
return exactly rows [N*S, (N+1)*S) of the result in a stable order, and the
internal bookkeeping column must never leak to the client.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.llm.spec import RegexSpec
from apps.sparkeng.engine import MATCH_COUNT_COLUMN, plan_transformation
from apps.sparkeng.results import build_page_index, count_matches, read_page, write_result

pytestmark = pytest.mark.django_db


@pytest.fixture
def written_result(spark, workdir: Path, settings):
    """120 rows written with at most 25 rows per file -> several chunks."""
    settings.SPARK_MAX_RECORDS_PER_FILE = 25
    rows = [(f"{i:04d}", f"user{i}@example.com") for i in range(120)]
    df = spark.createDataFrame(rows, ["ID", "Email"]).repartition(3)
    spec = RegexSpec(pattern=r"@example\.com")
    plan = plan_transformation(df, spec, ["Email"], "@redacted")

    out = workdir / "out"
    write_result(plan.dataframe, f"file://{out}")
    return out


def test_index_counts_every_row_and_hides_internal_columns(written_result: Path):
    chunks, total, columns = build_page_index(str(written_result))

    assert total == 120
    assert len(chunks) > 1, "maxRecordsPerFile should split the output"
    assert all(chunk.rows <= 25 for chunk in chunks)
    assert columns == ["ID", "Email"]
    assert MATCH_COUNT_COLUMN not in columns


def test_chunk_offsets_are_contiguous(written_result: Path):
    chunks, total, _ = build_page_index(str(written_result))
    cursor = 0
    for chunk in chunks:
        assert chunk.start == cursor
        cursor += chunk.rows
    assert cursor == total


def test_pages_tile_the_result_without_gaps_or_repeats(written_result: Path):
    chunks, total, _ = build_page_index(str(written_result))

    seen: list[str] = []
    page = 1
    while len(seen) < total:
        rows = read_page(chunks, page, 30)
        assert rows, f"page {page} came back empty"
        seen.extend(row["ID"] for row in rows)
        page += 1

    assert len(seen) == 120
    assert len(set(seen)) == 120, "no row may appear on two pages"


def test_page_size_larger_than_a_chunk_spans_files(written_result: Path):
    chunks, _, _ = build_page_index(str(written_result))
    rows = read_page(chunks, 1, 100)
    assert len(rows) == 100


def test_page_beyond_the_end_is_empty_not_an_error(written_result: Path):
    chunks, _, _ = build_page_index(str(written_result))
    assert read_page(chunks, 99, 50) == []


def test_rows_carry_the_transformation(written_result: Path):
    chunks, _, _ = build_page_index(str(written_result))
    rows = read_page(chunks, 1, 5)
    assert all(row["Email"].endswith("@redacted") for row in rows)
    assert MATCH_COUNT_COLUMN not in rows[0]


def test_match_count_is_read_back_from_parquet(spark, written_result: Path):
    assert count_matches(spark, f"file://{written_result}") == 120


def test_index_of_an_empty_prefix_is_empty(workdir: Path):
    chunks, total, columns = build_page_index(str(workdir / "nothing-here"))
    assert (chunks, total, columns) == ([], 0, [])
