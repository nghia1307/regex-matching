"""
SparkSession lifecycle and configuration.

One session per worker process, created lazily and reused. Creating a
SparkContext costs seconds and a JVM, and only one may exist per process, so the
Celery worker that owns it runs with ``--concurrency=1`` (see docker-compose).

Everything about talking to S3/MinIO lives here so no other module has to know
which one is behind ``s3a://``.
"""
from __future__ import annotations

import glob
import logging
import os
import threading
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

_session = None
_lock = threading.Lock()


def _endpoint_host(url: str | None) -> str:
    """Hadoop's fs.s3a.endpoint wants host[:port], not a full URL."""
    if not url:
        return ""
    parsed = urlparse(url if "//" in url else f"//{url}")
    return parsed.netloc or parsed.path


def _has_hadoop_cloud_committer() -> bool:
    """
    The S3A "magic" committer avoids rename-based commits, but it lives in the
    optional spark-hadoop-cloud jar. Detect it instead of assuming: enabling the
    committer without the jar fails every write with a ClassNotFoundException.
    """
    spark_home = os.environ.get("SPARK_HOME", "")
    if not spark_home:
        return False
    return bool(glob.glob(os.path.join(spark_home, "jars", "spark-hadoop-cloud*.jar")))


def spark_conf() -> dict[str, str]:
    """The full config map, kept in one place so it can be asserted in tests."""
    conf: dict[str, str] = {
        # --- sizing ---------------------------------------------------------
        "spark.driver.memory": settings.SPARK_DRIVER_MEMORY,
        "spark.executor.memory": settings.SPARK_EXECUTOR_MEMORY,
        "spark.executor.cores": str(settings.SPARK_EXECUTOR_CORES),
        # Shuffle partitions default to 200, which is badly wrong for a laptop
        # cluster: 200 tasks of a few rows each is pure scheduling overhead.
        "spark.sql.shuffle.partitions": str(settings.SPARK_SHUFFLE_PARTITIONS),
        # Split size for file scans -- this is what actually sets read
        # parallelism on a single large CSV.
        "spark.sql.files.maxPartitionBytes": getattr(
            settings, "SPARK_MAX_PARTITION_BYTES", "64m"
        ),
        # Adaptive execution coalesces the small partitions that inevitably
        # follow a filter, and picks join strategies at runtime.
        "spark.sql.adaptive.enabled": "true",
        "spark.sql.adaptive.coalescePartitions.enabled": "true",
        "spark.serializer": "org.apache.spark.serializer.KryoSerializer",
        "spark.local.dir": os.environ.get("SPARK_LOCAL_DIRS", "/tmp/spark-local"),
        # --- parquet output -------------------------------------------------
        "spark.sql.parquet.compression.codec": "snappy",
        "spark.sql.parquet.mergeSchema": "false",
        "spark.hadoop.parquet.enable.summary-metadata": "false",
        # Smaller row groups: page reads then touch one row group instead of a
        # whole file (see apps.sparkeng.results.read_page).
        "spark.hadoop.parquet.block.size": str(16 * 1024 * 1024),
        "spark.sql.sources.partitionOverwriteMode": "dynamic",
    }

    # --- S3A ---------------------------------------------------------------- #
    endpoint = _endpoint_host(settings.S3_ENDPOINT_URL)
    if endpoint:
        conf["spark.hadoop.fs.s3a.endpoint"] = endpoint
    conf.update(
        {
            "spark.hadoop.fs.s3a.impl": "org.apache.hadoop.fs.s3a.S3AFileSystem",
            "spark.hadoop.fs.s3a.endpoint.region": settings.AWS_DEFAULT_REGION,
            "spark.hadoop.fs.s3a.path.style.access": (
                "true" if settings.S3_PATH_STYLE else "false"
            ),
            "spark.hadoop.fs.s3a.connection.ssl.enabled": (
                "true" if settings.S3_USE_SSL else "false"
            ),
            "spark.hadoop.fs.s3a.connection.maximum": "64",
            "spark.hadoop.fs.s3a.fast.upload": "true",
            "spark.hadoop.fs.s3a.multipart.size": "67108864",
            "spark.hadoop.fs.s3a.attempts.maximum": "5",
            "spark.hadoop.fs.s3a.retry.limit": "5",
        }
    )

    # Credentials: explicit keys locally (MinIO), the default provider chain in
    # AWS. Leaving the provider *unset* is what makes the EC2 instance profile
    # work -- Hadoop's default chain ends in IAMInstanceCredentialsProvider, so
    # the deployment needs no long-lived access keys anywhere.
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        conf.update(
            {
                "spark.hadoop.fs.s3a.aws.credentials.provider": (
                    "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider"
                ),
                "spark.hadoop.fs.s3a.access.key": settings.AWS_ACCESS_KEY_ID,
                "spark.hadoop.fs.s3a.secret.key": settings.AWS_SECRET_ACCESS_KEY,
            }
        )

    if _has_hadoop_cloud_committer():
        # Rename-free commit: correct and fast on real S3.
        conf.update(
            {
                "spark.hadoop.fs.s3a.committer.name": "magic",
                "spark.sql.sources.commitProtocolClass": (
                    "org.apache.spark.internal.io.cloud.PathOutputCommitProtocol"
                ),
                "spark.sql.parquet.output.committer.class": (
                    "org.apache.spark.internal.io.cloud.BindingParquetOutputCommitter"
                ),
            }
        )
    else:
        # Fall back to the v2 FileOutputCommitter: one rename per file instead of
        # a second full copy of the output directory. MinIO does server-side
        # copies, so this is cheap locally.
        conf["spark.hadoop.mapreduce.fileoutputcommitter.algorithm.version"] = "2"

    # --- cluster networking -------------------------------------------------- #
    # In client mode the executors dial the driver back by hostname, so the
    # driver must advertise a name that resolves on the compose network.
    if not settings.SPARK_MASTER_URL.startswith("local") and settings.SPARK_DRIVER_HOST:
        conf.update(
            {
                "spark.driver.host": settings.SPARK_DRIVER_HOST,
                "spark.driver.bindAddress": "0.0.0.0",
                "spark.driver.port": "7078",
                "spark.blockManager.port": "7079",
            }
        )
    return conf


def get_spark(app_name: str = "regex-nl-platform"):
    """Return the process-wide SparkSession, creating it on first use."""
    global _session
    if _session is not None:
        return _session

    with _lock:
        if _session is not None:
            return _session

        from pyspark.sql import SparkSession

        builder = SparkSession.builder.appName(app_name).master(settings.SPARK_MASTER_URL)
        for key, value in spark_conf().items():
            builder = builder.config(key, value)

        logger.info(
            "starting SparkSession master=%s", settings.SPARK_MASTER_URL
        )
        _session = builder.getOrCreate()
        _session.sparkContext.setLogLevel(settings.SPARK_LOG_LEVEL)
        logger.info(
            "SparkSession ready version=%s ui=%s",
            _session.version,
            _session.sparkContext.uiWebUrl,
        )
        return _session


def stop_spark() -> None:
    """Only used by tests and worker shutdown hooks."""
    global _session
    with _lock:
        if _session is not None:
            _session.stop()
            _session = None


def cluster_status() -> dict[str, object]:
    """Small health payload for /api/health -- never starts a session."""
    if _session is None:
        return {"session": "not started", "master": settings.SPARK_MASTER_URL}
    sc = _session.sparkContext
    return {
        "session": "running",
        "master": settings.SPARK_MASTER_URL,
        "version": _session.version,
        "application_id": sc.applicationId,
        "ui": sc.uiWebUrl,
        "default_parallelism": sc.defaultParallelism,
    }
