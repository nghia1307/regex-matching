"""Google Gemini, via the google-genai SDK, constrained to a JSON schema."""
from __future__ import annotations

from django.conf import settings

from ..prompts import SYSTEM_INSTRUCTION, build_prompt
from ..spec import RESPONSE_SCHEMA, RegexRequest, RegexSpec
from .base import LLMError, LLMTransientError, parse_json_response, spec_from_payload


class GeminiProvider:
    """Google Gemini via the google-genai SDK, constrained to a JSON schema."""

    name = "gemini"

    def __init__(self, api_key: str = "", model: str = "") -> None:
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model = model or settings.GEMINI_MODEL
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._client = None

    def _client_or_create(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover
                raise LLMError("google-genai is not installed") from exc
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _config(self):
        from google.genai import types

        kwargs = dict(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.0,
            candidate_count=1,
            # Generous on purpose. Current Gemini models spend reasoning tokens
            # from this same budget -- a 2k cap looks sufficient for a 300-token
            # JSON answer and then truncates it mid-string, which surfaces as a
            # baffling "non-JSON output" error. See _check_finish_reason.
            max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
            # No tools are passed, so automatic function calling is dead weight.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        # http_options moved around between SDK versions; a timeout is important
        # enough to attempt, but not important enough to break on.
        try:
            return types.GenerateContentConfig(
                http_options=types.HttpOptions(
                    timeout=int(settings.LLM_TIMEOUT_SECONDS * 1000)
                ),
                **kwargs,
            )
        except (TypeError, AttributeError):  # pragma: no cover
            return types.GenerateContentConfig(**kwargs)

    def generate(self, request: RegexRequest, repair_hint: str = "") -> RegexSpec:
        client = self._client_or_create()
        prompt = build_prompt(request, repair_hint=repair_hint)

        try:
            response = client.models.generate_content(
                model=self.model, contents=prompt, config=self._config()
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide range
            message = str(exc)
            transient = any(
                token in message.lower()
                for token in ("429", "resource_exhausted", "timeout", "deadline", "503", "500", "unavailable")
            )
            error = LLMTransientError if transient else LLMError
            raise error(f"gemini call failed: {message}") from exc

        _check_finish_reason(response)
        payload = parse_json_response(getattr(response, "text", None))
        return spec_from_payload(payload, request, provider=self.name, model=self.model)


def _check_finish_reason(response) -> None:
    """
    Turn a truncated or blocked response into an accurate error.

    Without this, hitting the output-token ceiling produces half a JSON document
    and the failure gets misreported as "the model returned non-JSON", sending
    you looking in entirely the wrong place.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise LLMError("model returned no candidates (possibly a safety block)")

    reason = str(getattr(candidates[0], "finish_reason", "") or "")
    if "MAX_TOKENS" in reason:
        usage = getattr(response, "usage_metadata", None)
        total = getattr(usage, "total_token_count", "?") if usage else "?"
        raise LLMTransientError(
            "response was truncated at the output-token limit "
            f"({settings.GEMINI_MAX_OUTPUT_TOKENS}, total spent {total}); "
            "raise GEMINI_MAX_OUTPUT_TOKENS"
        )
    if "SAFETY" in reason or "BLOCK" in reason or "RECITATION" in reason:
        raise LLMError(f"model refused to answer (finish_reason={reason})")
