"""OpenAI, via the openai SDK, constrained to a JSON schema."""
from __future__ import annotations

from django.conf import settings

from ..prompts import SYSTEM_INSTRUCTION, build_prompt
from ..spec import RegexRequest, RegexSpec
from .base import LLMError, LLMTransientError, parse_json_response, spec_from_payload, strict_schema


class OpenAIProvider:
    """OpenAI via the openai SDK, constrained to a JSON schema."""

    name = "openai"

    def __init__(self, api_key: str = "", model: str = "") -> None:
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model or settings.OPENAI_MODEL
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        self._client = None

    def _client_or_create(self):
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover
                raise LLMError("openai is not installed") from exc
            self._client = openai.OpenAI(
                api_key=self.api_key, timeout=settings.LLM_TIMEOUT_SECONDS
            )
        return self._client

    def generate(self, request: RegexRequest, repair_hint: str = "") -> RegexSpec:
        import openai

        client = self._client_or_create()
        prompt = build_prompt(request, repair_hint=repair_hint)

        try:
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=settings.OPENAI_MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_INSTRUCTION},
                    {"role": "user", "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "regex_spec",
                        "schema": strict_schema(),
                        "strict": True,
                    },
                },
            )
        except openai.RateLimitError as exc:
            raise LLMTransientError(f"openai call failed: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise LLMTransientError(f"openai call failed: {exc}") from exc
        except openai.APIStatusError as exc:
            error = LLMTransientError if exc.status_code >= 500 else LLMError
            raise error(f"openai call failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide range
            raise LLMError(f"openai call failed: {exc}") from exc

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMTransientError(
                "response was truncated at the output-token limit "
                f"({settings.OPENAI_MAX_OUTPUT_TOKENS}); raise OPENAI_MAX_OUTPUT_TOKENS"
            )
        if choice.finish_reason == "content_filter":
            raise LLMError("model refused to answer (content_filter)")

        payload = parse_json_response(choice.message.content)
        return spec_from_payload(payload, request, provider=self.name, model=self.model)
