# =============================================================================
# One image, many roles (api / celery worker / spark master / spark worker /
# flower / seeder). Building once guarantees the driver and the executors run
# byte-identical PySpark + Hadoop + JVM versions, which is the single most
# common source of "works on my machine" Spark failures.
# =============================================================================
FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    SPARK_HOME=/usr/local/lib/python3.11/site-packages/pyspark \
    PYSPARK_PYTHON=/usr/local/bin/python \
    PYSPARK_DRIVER_PYTHON=/usr/local/bin/python

# Spark 3.5 runs on Java 17. procps/curl are needed by Spark's own scripts and
# by container healthchecks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openjdk-17-jre-headless \
      procps curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# --- S3A connector -----------------------------------------------------------
# PySpark 3.5.x bundles Hadoop 3.3.4, so hadoop-aws MUST be 3.3.4 and its
# aws-java-sdk-bundle MUST be 1.12.262 (the exact pair Hadoop 3.3.4 was built
# against). Mixing versions produces opaque NoClassDefFoundError at read time.
ARG HADOOP_AWS_VERSION=3.3.4
ARG AWS_SDK_VERSION=1.12.262
RUN set -eux; \
    test -d "$SPARK_HOME/jars" || { echo "SPARK_HOME wrong: $SPARK_HOME"; exit 1; }; \
    curl -fsSL -o "$SPARK_HOME/jars/hadoop-aws-${HADOOP_AWS_VERSION}.jar" \
      "https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/${HADOOP_AWS_VERSION}/hadoop-aws-${HADOOP_AWS_VERSION}.jar"; \
    curl -fsSL -o "$SPARK_HOME/jars/aws-java-sdk-bundle-${AWS_SDK_VERSION}.jar" \
      "https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/${AWS_SDK_VERSION}/aws-java-sdk-bundle-${AWS_SDK_VERSION}.jar"; \
    ls -1 "$SPARK_HOME/jars" | grep -E 'hadoop-(aws|client)|aws-java-sdk'

# Non-root: Spark writes scratch files, so give it a home it owns.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /tmp/spark-local /app/staticfiles \
 && chown -R appuser:appuser /tmp/spark-local /app

COPY --chown=appuser:appuser . /app

USER appuser
ENV HOME=/home/appuser

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
