"""
Shared Django settings.

Configuration is read from the environment only -- the same image runs as the
API, the Celery worker, the Spark driver and the seeder, so behaviour must be
switchable without rebuilding.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# small env helpers
# --------------------------------------------------------------------------- #
def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# core django
# --------------------------------------------------------------------------- #
SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,backend")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "apps.jobs",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.jobs.middleware.RequestIdMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "regexdb"),
        "USER": env("POSTGRES_USER", "regex"),
        "PASSWORD": env("POSTGRES_PASSWORD", "regexpass"),
        "HOST": env("POSTGRES_HOST", "postgres"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
USE_TZ = True
TIME_ZONE = "UTC"

CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
)

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "apps.jobs.exceptions.api_exception_handler",
    "DEFAULT_PAGINATION_CLASS": "apps.jobs.pagination.JobListPagination",
    "PAGE_SIZE": 20,
    "UNAUTHENTICATED_USER": None,
}


# --------------------------------------------------------------------------- #
# redis / celery
# --------------------------------------------------------------------------- #
REDIS_URL = env("REDIS_URL", "redis://redis:6379/0")
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://redis:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://redis:6379/2")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "regexapp",
    }
}


# --------------------------------------------------------------------------- #
# object storage (MinIO locally / Amazon S3 in the cloud -- same code path)
# --------------------------------------------------------------------------- #
S3_ENDPOINT_URL = env("S3_ENDPOINT_URL") or None
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
AWS_DEFAULT_REGION = env("AWS_DEFAULT_REGION", "us-east-1")
S3_BUCKET = env("S3_BUCKET", "regex-data")
S3_RAW_PREFIX = env("S3_RAW_PREFIX", "raw/")
S3_RESULT_PREFIX = env("S3_RESULT_PREFIX", "results/")
S3_USE_SSL = env_bool("S3_USE_SSL", False)
S3_PATH_STYLE = env_bool("S3_PATH_STYLE", True)


# --------------------------------------------------------------------------- #
# spark
# --------------------------------------------------------------------------- #
SPARK_MASTER_URL = env("SPARK_MASTER_URL", "local[*]")
SPARK_DRIVER_HOST = env("SPARK_DRIVER_HOST") or None
SPARK_DRIVER_MEMORY = env("SPARK_DRIVER_MEMORY", "1g")
SPARK_EXECUTOR_MEMORY = env("SPARK_EXECUTOR_MEMORY", "1g")
SPARK_EXECUTOR_CORES = env_int("SPARK_EXECUTOR_CORES", 2)
SPARK_SHUFFLE_PARTITIONS = env_int("SPARK_SHUFFLE_PARTITIONS", 8)
SPARK_MAX_PARTITION_BYTES = env("SPARK_MAX_PARTITION_BYTES", "64m")
SPARK_MAX_RECORDS_PER_FILE = env_int("SPARK_MAX_RECORDS_PER_FILE", 100000)
SPARK_LOG_LEVEL = env("SPARK_LOG_LEVEL", "WARN")
EXCEL_MAX_BYTES = env_int("EXCEL_MAX_BYTES", 100 * 1024 * 1024)


# --------------------------------------------------------------------------- #
# llm
# --------------------------------------------------------------------------- #
# gemini | anthropic | openai -- see apps.llm.providers.build_provider
LLM_PROVIDER = env("LLM_PROVIDER", "gemini")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_MAX_OUTPUT_TOKENS = env_int("GEMINI_MAX_OUTPUT_TOKENS", 8192)
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-opus-5")
ANTHROPIC_MAX_OUTPUT_TOKENS = env_int("ANTHROPIC_MAX_OUTPUT_TOKENS", 8192)
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4.1")
OPENAI_MAX_OUTPUT_TOKENS = env_int("OPENAI_MAX_OUTPUT_TOKENS", 8192)
LLM_CACHE_TTL_SECONDS = env_int("LLM_CACHE_TTL_SECONDS", 7 * 24 * 3600)
LLM_TIMEOUT_SECONDS = env_int("LLM_TIMEOUT_SECONDS", 30)


# --------------------------------------------------------------------------- #
# safety limits
# --------------------------------------------------------------------------- #
REGEX_MAX_LENGTH = env_int("REGEX_MAX_LENGTH", 2000)
REGEX_TIMEOUT_MS = env_int("REGEX_TIMEOUT_MS", 1000)
API_MAX_PAGE_SIZE = env_int("API_MAX_PAGE_SIZE", 500)


# --------------------------------------------------------------------------- #
# logging -- structured so container logs stay greppable / shippable
# --------------------------------------------------------------------------- #
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
        "plain": {"format": "%(asctime)s %(levelname)-7s %(name)s | %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": env("LOG_FORMAT", "plain"),
        }
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "apps": {"level": env("LOG_LEVEL", "INFO"), "propagate": True},
        "py4j": {"level": "WARNING", "propagate": True},
        "django.db.backends": {"level": "WARNING", "propagate": True},
    },
}
