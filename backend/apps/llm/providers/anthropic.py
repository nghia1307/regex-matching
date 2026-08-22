"""Anthropic Claude, via the anthropic SDK, constrained to a JSON schema."""
from __future__ import annotations

from django.conf import settings

from ..prompts import SYSTEM_INSTRUCTION, build_prompt
from ..spec import RegexRequest, RegexSpec
from .base import LLMError, LLMTransientError, parse_json_response, spec_from_payload, strict_schema


class AnthropicProvider:
    """Anthropic Claude via the anthropic SDK, constrained to a JSON schema."""

    name = "anthropic"

    def __init__(self, api_key: str = "", model: str = "") -> None:
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.model = model or settings.ANTHROPIC_MODEL
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._client = None

    def _client_or_create(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise LLMError("anthropic is not installed") from exc
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=settings.LLM_TIMEOUT_SECONDS
            )
        return self._client

    def generate(self, request: RegexRequest, repair_hint: str = "") -> RegexSpec:
        import anthropic

        client = self._client_or_create()
        prompt = build_prompt(request, repair_hint=repair_hint)

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=settings.ANTHROPIC_MAX_OUTPUT_TOKENS,
                system=SYSTEM_INSTRUCTION,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": strict_schema()}
                },
            )
        except anthropic.RateLimitError as exc:
            raise LLMTransientError(f"anthropic call failed: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransientError(f"anthropic call failed: {exc}") from exc
        except anthropic.APIStatusError as exc:
            error = LLMTransientError if exc.status_code >= 500 else LLMError
            raise error(f"anthropic call failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide range
            raise LLMError(f"anthropic call failed: {exc}") from exc

        _check_stop_reason(response)
        text = next((b.text for b in response.content if b.type == "text"), None)
        payload = parse_json_response(text)
        return spec_from_payload(payload, request, provider=self.name, model=self.model)


def _check_stop_reason(response) -> None:
    reason = getattr(response, "stop_reason", "")
    if reason == "max_tokens":
        raise LLMTransientError(
            "response was truncated at the output-token limit "
            f"({settings.ANTHROPIC_MAX_OUTPUT_TOKENS}); "
            "raise ANTHROPIC_MAX_OUTPUT_TOKENS"
        )
    if reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise LLMError(f"model refused to answer (category={category})")
