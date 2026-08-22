"""
Test settings: SQLite, no network, in-process Spark.

The LLM provider itself is monkeypatched to a fake in ``tests/conftest.py`` --
keeping tests hermetic means the task/Spark layer can be exercised in CI
without MinIO, Redis or a Gemini key.
"""
import tempfile

from .base import *  # noqa: F401,F403

DEBUG = False

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-cache",
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

LLM_PROVIDER = "gemini"
GEMINI_API_KEY = "test-key"

# In-process Spark: no cluster, no S3A, small partition counts so the tests
# stay fast while still exercising the real multi-partition code paths.
SPARK_MASTER_URL = "local[2]"
SPARK_DRIVER_HOST = None
SPARK_SHUFFLE_PARTITIONS = 2
SPARK_MAX_RECORDS_PER_FILE = 25
SPARK_LOG_LEVEL = "ERROR"

TEST_TMP_DIR = tempfile.mkdtemp(prefix="regexapp-tests-")
