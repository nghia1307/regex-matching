"""
Shared contract and helpers for every LLM provider.

Every provider returns the same JSON shape (see ``apps.llm.spec.RESPONSE_SCHEMA``),
so parsing that JSON into a :class:`~apps.llm.spec.RegexSpec` lives here once
instead of being copy-pasted into each vendor module.
"""
from __future__ import annotations

import json
import re
from typing import Protocol

from ..spec import RESPONSE_SCHEMA, RegexRequest, RegexSpec


class LLMError(RuntimeError):
    """Provider-level failure (network, quota, malformed output)."""


class LLMTransientError(LLMError):
    """Worth retrying: timeouts, 429s, 5xx."""


class LLMProvider(Protocol):
    name: str
    model: str

    def generate(self, request: RegexRequest, repair_hint: str = "") -> RegexSpec: ...


def strict_schema() -> dict:
    """``RESPONSE_SCHEMA`` plus the ``additionalProperties: false`` that
    Anthropic's and OpenAI's strict json-schema modes require."""
    return {**RESPONSE_SCHEMA, "additionalProperties": False}


def parse_json_response(text: str | None) -> dict:
    if not text or not text.strip():
        raise LLMError("model returned an empty response")
    cleaned = text.strip()
    # Defensive: response_schema normally prevents fences, but a stray one is
    # cheaper to strip than to debug.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned non-JSON output: {cleaned[:200]}") from exc
    if not isinstance(payload, dict):
        raise LLMError("model returned JSON that is not an object")
    return payload


def spec_from_payload(
    payload: dict, request: RegexRequest, *, provider: str, model: str
) -> RegexSpec:
    pattern = (payload.get("pattern") or "").strip()
    if not pattern:
        raise LLMError("model returned no pattern")

    def _string_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v) for v in value if isinstance(v, (str, int, float))][:8]

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    try:
        group = int(payload.get("group") or 0)
    except (TypeError, ValueError):
        group = 0

    return RegexSpec(
        pattern=pattern,
        operation=request.operation,
        case_insensitive=bool(payload.get("case_insensitive")),
        explanation=str(payload.get("explanation") or "").strip(),
        confidence=max(0.0, min(confidence, 1.0)),
        should_match=_string_list(payload.get("should_match")),
        should_not_match=_string_list(payload.get("should_not_match")),
        replacement_template=str(payload.get("replacement_template") or ""),
        group=group,
        provider=provider,
        model=model,
    )
