"""
Request validation and response shaping.

Validation here is the first line of defence: it is where a request is rejected
before it can reach a Celery worker, a Spark cluster or the LLM. Anything that
can be checked without touching the data is checked here.
"""
from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

from apps.llm.spec import Operation
from apps.storage import s3

from .models import Job
from .services import get_progress


class JobCreateSerializer(serializers.Serializer):
    """The submit payload."""

    source_key = serializers.CharField(max_length=1024)
    sheet_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    operation = serializers.ChoiceField(
        choices=Operation.ALL, required=False, default=Operation.REPLACE
    )
    natural_language = serializers.CharField(max_length=2000)
    replacement_value = serializers.CharField(
        max_length=1024, required=False, allow_blank=True, default=""
    )
    target_columns = serializers.ListField(
        child=serializers.CharField(max_length=255), min_length=1, max_length=50
    )
    force_refresh = serializers.BooleanField(required=False, default=False)

    def validate_source_key(self, value: str) -> str:
        value = value.strip().lstrip("/")
        if ".." in value:
            raise serializers.ValidationError("invalid key")
        if s3.extension_of(value) not in s3.SUPPORTED_EXTENSIONS:
            raise serializers.ValidationError(
                f"unsupported file type; expected one of {', '.join(s3.SUPPORTED_EXTENSIONS)}"
            )
        # Fail fast on a missing object rather than queueing a job that cannot run.
        s3.head_object(value)
        return value

    def validate_natural_language(self, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise serializers.ValidationError(
                "describe the pattern in a few words, e.g. 'find email addresses'"
            )
        return value

    def validate_target_columns(self, value: list[str]) -> list[str]:
        cleaned = [column.strip() for column in value if column and column.strip()]
        if not cleaned:
            raise serializers.ValidationError("select at least one column")
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(cleaned))

    def validate(self, attrs: dict) -> dict:
        operation = attrs.get("operation", Operation.REPLACE)
        if operation in Operation.NEEDS_REPLACEMENT and not attrs.get("replacement_value"):
            raise serializers.ValidationError(
                {"replacement_value": "required for the REPLACE operation"}
            )
        return attrs


class JobSerializer(serializers.ModelSerializer):
    """Full job state -- what the UI polls."""

    progress = serializers.SerializerMethodField()
    phase = serializers.SerializerMethodField()
    duration_seconds = serializers.FloatField(read_only=True)
    queue_wait_seconds = serializers.FloatField(read_only=True)
    regex = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = (
            "id",
            "status",
            "progress",
            "phase",
            "source_key",
            "sheet_name",
            "operation",
            "natural_language",
            "replacement_value",
            "target_columns",
            "regex",
            "result_columns",
            "added_columns",
            "total_rows",
            "matched_cells",
            "error_message",
            "error_type",
            "attempt",
            "created_at",
            "started_at",
            "finished_at",
            "duration_seconds",
            "queue_wait_seconds",
        )
        read_only_fields = fields

    def get_progress(self, obj: Job) -> int:
        return get_progress(obj)[0]

    def get_phase(self, obj: Job) -> str:
        return get_progress(obj)[1]

    def get_regex(self, obj: Job) -> dict:
        """Everything about the generated pattern, grouped for the UI."""
        return {
            "pattern": obj.regex_pattern,
            "case_insensitive": obj.regex_case_insensitive,
            "replacement_template": obj.replacement_template,
            "group": obj.extract_group,
            "provider": obj.llm_provider,
            "model": obj.llm_model,
            "explanation": obj.llm_explanation,
            "confidence": obj.llm_confidence,
            "cached": obj.llm_cached,
            "warnings": obj.llm_warnings,
            "self_test_passed": obj.self_test_passed,
        }


class JobSummarySerializer(serializers.ModelSerializer):
    """Compact shape for the job list."""

    progress = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = (
            "id",
            "status",
            "progress",
            "operation",
            "source_key",
            "natural_language",
            "total_rows",
            "matched_cells",
            "created_at",
            "duration_seconds",
        )

    def get_progress(self, obj: Job) -> int:
        return get_progress(obj)[0]


class PageQuerySerializer(serializers.Serializer):
    """Result pagination parameters."""

    page = serializers.IntegerField(min_value=1, required=False, default=1)
    page_size = serializers.IntegerField(
        min_value=1, required=False, default=50
    )

    def validate_page_size(self, value: int) -> int:
        return min(value, settings.API_MAX_PAGE_SIZE)
