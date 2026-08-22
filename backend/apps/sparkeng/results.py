"""
Result materialisation and paging.

The brief is explicit that millions of rows must not be shipped to the browser,
which really means: the result must live somewhere that supports cheap random
access. The design here is

    transform -> write Parquet (bounded file size) -> build a page index
              -> serve pages straight out of Parquet with pyarrow

and the important consequence is that **serving a page needs neither Spark nor
Celery**. Django answers a page request by reading one row group out of one
Parquet file, so paging stays in the low tens of milliseconds whether the result
has 3 rows or 50 million, and the Spark cluster is free to run the next job.

The page index is built from Parquet *footers* (``num_rows`` per row group),
which is metadata only -- no data pages are read, so indexing a 5 GB result costs
a few hundred small range requests, not a scan.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Iterator
from urllib.parse import urlparse

from django.conf import settings
from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from apps.sparkeng.engine import INTERNAL_PREFIX, MATCH_COUNT_COLUMN

logger = logging.getLogger(__name__)


@dataclass
class ChunkRef:
    """One Parquet file in the result, with its position in the global order."""

    path: str
    rows: int
    start: int  # 0-based index of this file's first row

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def result_prefix(job_id: str) -> str:
    return f"{settings.S3_RESULT_PREFIX.rstrip('/')}/{job_id}"


def result_uri(job_id: str) -> str:
    """Spark-side location of a job's output."""
    from apps.storage import s3

    return s3.s3a_uri(result_prefix(job_id))


def pyarrow_prefix(job_id: str) -> str:
    """pyarrow addresses objects as 'bucket/key', with no URI scheme."""
    return f"{settings.S3_BUCKET}/{result_prefix(job_id)}"


def write_result(df: DataFrame, uri: str) -> None:
    """
    Write the transformed frame as Parquet.

    ``maxRecordsPerFile`` is the load-bearing option: it caps every output file
    at a known row count, which is what makes the page index predictable and
    keeps any single page read small. Without it Spark writes one file per
    partition, and partition sizes vary wildly with input skew.
    """
    (
        df.write.mode("overwrite")
        .option("maxRecordsPerFile", settings.SPARK_MAX_RECORDS_PER_FILE)
        .option("compression", "snappy")
        .parquet(uri)
    )


def count_matches(spark, uri: str) -> int:
    """
    Total affected cells, read back from the written result.

    This is a second pass, but a *columnar* one: Parquet lets Spark read only the
    ``__matched`` column and skip every other byte, so it costs a fraction of the
    transformation. Doing it this way avoids caching the input frame in memory
    purely to count and transform in one go.
    """
    row = spark.read.parquet(uri).agg(F.sum(MATCH_COUNT_COLUMN).alias("total")).collect()
    return int(row[0]["total"] or 0) if row else 0


# --------------------------------------------------------------------------- #
# pyarrow side: index + page reads, no JVM involved
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _filesystem():
    """A pyarrow S3 filesystem pointed at MinIO or AWS, mirroring settings."""
    from pyarrow import fs as pafs

    endpoint = settings.S3_ENDPOINT_URL
    if not endpoint:
        return pafs.S3FileSystem(
            access_key=settings.AWS_ACCESS_KEY_ID or None,
            secret_key=settings.AWS_SECRET_ACCESS_KEY or None,
            region=settings.AWS_DEFAULT_REGION,
        )

    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    return pafs.S3FileSystem(
        access_key=settings.AWS_ACCESS_KEY_ID or None,
        secret_key=settings.AWS_SECRET_ACCESS_KEY or None,
        region=settings.AWS_DEFAULT_REGION,
        endpoint_override=parsed.netloc or parsed.path,
        scheme="https" if settings.S3_USE_SSL else "http",
    )


def _open(path: str):
    """Open a result file. ``path`` is 'bucket/key' on S3, or a local path."""
    if path.startswith("/") or path.startswith("./"):
        return open(path, "rb")  # noqa: SIM115 - caller closes via context manager
    return _filesystem().open_input_file(path)


def _list_parquet_files(prefix: str) -> list[str]:
    """Sorted list of result files. Local paths are supported for tests."""
    if prefix.startswith("/") or prefix.startswith("./"):
        import glob
        import os

        return sorted(glob.glob(os.path.join(prefix, "**", "*.parquet"), recursive=True))

    from pyarrow import fs as pafs

    selector = pafs.FileSelector(prefix, recursive=True, allow_not_found=True)
    infos = _filesystem().get_file_info(selector)
    return sorted(
        info.path
        for info in infos
        if info.type == pafs.FileType.File and info.path.endswith(".parquet")
    )


def build_page_index(prefix: str) -> tuple[list[ChunkRef], int, list[str]]:
    """
    Return ``(chunks, total_rows, columns)`` for a finished result.

    ``prefix`` is 'bucket/results/<job-id>' (or a local directory in tests).
    Files are ordered lexicographically, which matches Spark's
    ``part-00000, part-00001, ...`` naming and therefore preserves the input
    partition order -- i.e. rows come back in the order they were read.
    """
    import pyarrow.parquet as pq

    chunks: list[ChunkRef] = []
    columns: list[str] = []
    cursor = 0

    for path in _list_parquet_files(prefix):
        with _open(path) as handle:
            parquet_file = pq.ParquetFile(handle)
            rows = parquet_file.metadata.num_rows
            if not columns:
                columns = [
                    name
                    for name in parquet_file.schema_arrow.names
                    if not name.startswith(INTERNAL_PREFIX)
                ]
        if rows == 0:
            continue
        chunks.append(ChunkRef(path=path, rows=rows, start=cursor))
        cursor += rows

    logger.info("page index built files=%s rows=%s", len(chunks), cursor)
    return chunks, cursor, columns


def _chunks_for_slice(
    chunks: list[ChunkRef], offset: int, limit: int
) -> Iterator[tuple[ChunkRef, int, int]]:
    """Yield ``(chunk, local_offset, take)`` for the requested global slice."""
    end = offset + limit
    for chunk in chunks:
        chunk_end = chunk.start + chunk.rows
        if chunk_end <= offset or chunk.start >= end:
            continue
        local_offset = max(offset - chunk.start, 0)
        take = min(chunk_end, end) - (chunk.start + local_offset)
        if take > 0:
            yield chunk, local_offset, take


def read_page(
    chunks: list[ChunkRef],
    page: int,
    page_size: int,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Read one page of rows.

    Only the row groups that overlap the requested slice are decoded, and only
    the requested columns, so the cost tracks ``page_size`` rather than result
    size.
    """
    import pyarrow.parquet as pq

    offset = max(page - 1, 0) * page_size
    rows: list[dict[str, Any]] = []

    for chunk, local_offset, take in _chunks_for_slice(chunks, offset, page_size):
        with _open(chunk.path) as handle:
            parquet_file = pq.ParquetFile(handle)
            wanted = [
                name
                for name in parquet_file.schema_arrow.names
                if not name.startswith(INTERNAL_PREFIX)
                and (not columns or name in columns)
            ]

            # Walk row groups, skipping any that fall outside the slice.
            cursor = 0
            remaining_skip = local_offset
            remaining_take = take
            for group_index in range(parquet_file.num_row_groups):
                group_rows = parquet_file.metadata.row_group(group_index).num_rows
                if remaining_skip >= group_rows:
                    remaining_skip -= group_rows
                    cursor += group_rows
                    continue
                table = parquet_file.read_row_group(group_index, columns=wanted)
                sliced = table.slice(remaining_skip, remaining_take)
                rows.extend(sliced.to_pylist())
                remaining_take -= sliced.num_rows
                remaining_skip = 0
                cursor += group_rows
                if remaining_take <= 0:
                    break

    return [_stringify(row) for row in rows]


def _stringify(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe values; booleans stay booleans so the UI can style them."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        else:
            out[key] = str(value)
    return out
