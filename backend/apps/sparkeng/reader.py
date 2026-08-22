"""
Source readers: S3 object -> Spark DataFrame.

CSV/TSV and Parquet are read by Spark directly over ``s3a://``, so the bytes go
straight from S3 to the executors and are split across partitions -- the web
process never touches them.

Excel is the exception, and deliberately so. There is no native Spark reader for
.xlsx: the format is a zip of XML that cannot be split, so *something* has to
parse it in one place. We do that in the driver with openpyxl/pandas under a hard
size cap, then hand the frame to Spark and repartition it. Since the XLSX format
caps out at ~1.05M rows per sheet, Excel can never be the millions-of-rows path
anyway -- CSV and Parquet are.
"""
from __future__ import annotations

import io
import logging

from django.conf import settings
from pyspark.sql import DataFrame, SparkSession

from apps.storage import s3

logger = logging.getLogger(__name__)

_SCHEMES = ("s3a://", "s3://", "file://", "/")


def resolve_uri(key_or_uri: str) -> str:
    """Accept either a bucket key or a full URI (tests use ``file://``)."""
    if key_or_uri.startswith(_SCHEMES):
        return key_or_uri.replace("s3://", "s3a://", 1)
    return s3.s3a_uri(key_or_uri)


def read_source(
    spark: SparkSession, key_or_uri: str, sheet: str | None = None
) -> DataFrame:
    """Load a source file with every column typed as string.

    Schema inference is switched off on purpose. It costs an extra full pass over
    the data, and typing is actively unhelpful here: regex work is textual, and
    inferred types silently mangle values (leading zeros in postcodes, long
    account numbers becoming floats in scientific notation).
    """
    uri = resolve_uri(key_or_uri)
    extension = s3.extension_of(uri)

    if extension in s3.EXCEL_EXTENSIONS:
        return _read_excel(spark, key_or_uri, sheet=sheet)
    if extension == ".parquet":
        return spark.read.parquet(uri)
    if extension in {".csv", ".tsv"}:
        return _read_delimited(spark, uri, extension)
    raise ValueError(f"unsupported file type {extension!r} for {key_or_uri}")


def _read_delimited(spark: SparkSession, uri: str, extension: str) -> DataFrame:
    separator = "\t" if extension == ".tsv" else ","
    reader = (
        spark.read.option("header", "true")
        .option("inferSchema", "false")
        .option("sep", separator)
        .option("quote", '"')
        .option("escape", '"')
        .option("multiLine", "false")
        .option("encoding", "UTF-8")
        # PERMISSIVE keeps a malformed row instead of failing the whole job; a
        # single bad line in a 5M-row file should not lose the other 4,999,999.
        .option("mode", "PERMISSIVE")
        .option("columnNameOfCorruptRecord", "__corrupt_record")
    )
    df = reader.csv(uri)
    if "__corrupt_record" in df.columns:
        df = df.drop("__corrupt_record")
    return df


def _read_excel(
    spark: SparkSession, key_or_uri: str, sheet: str | None = None
) -> DataFrame:
    import pandas as pd

    if "://" in key_or_uri:
        raise ValueError("Excel sources must be plain bucket keys")

    payload = s3.download_bytes(key_or_uri, settings.EXCEL_MAX_BYTES)
    frame = pd.read_excel(
        io.BytesIO(payload),
        sheet_name=sheet or 0,
        dtype=str,
        keep_default_na=False,
        na_values=[],
        engine="openpyxl",
    )
    frame.columns = [str(column).strip() for column in frame.columns]
    frame = frame.fillna("")

    if frame.empty:
        raise ValueError(f"{key_or_uri} contains no data rows")

    logger.info("excel loaded rows=%s cols=%s", len(frame), len(frame.columns))

    df = spark.createDataFrame(frame)
    # createDataFrame yields very few partitions; spread the work out before the
    # transformation so the cluster is actually used.
    target_partitions = max(spark.sparkContext.defaultParallelism, 2)
    return df.repartition(target_partitions)
