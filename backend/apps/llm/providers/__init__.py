"""
LLM providers.

The rest of the app depends on the :class:`LLMProvider` protocol, never on a
vendor SDK directly. That keeps swapping providers a config change
(``LLM_PROVIDER``) rather than a rewrite. Each provider lives in its own
module:

* :mod:`.gemini`    -- :class:`GeminiProvider`, Google Gemini.
* :mod:`.anthropic` -- :class:`AnthropicProvider`, Anthropic Claude.
* :mod:`.openai`    -- :class:`OpenAIProvider`, OpenAI.
"""
from __future__ import annotations

from django.conf import settings

from .anthropic import AnthropicProvider
from .base import LLMError, LLMProvider, LLMTransientError
from .gemini import GeminiProvider
from .openai import OpenAIProvider

__all__ = [
    "LLMError",
    "LLMTransientError",
    "LLMProvider",
    "GeminiProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "build_provider",
]


def build_provider(name: str = "") -> LLMProvider:
    """Factory used by the service layer; keeps provider choice in settings."""
    name = (name or settings.LLM_PROVIDER or "gemini").strip().lower()
    if name == "gemini":
        return GeminiProvider()
    if name in {"anthropic", "claude"}:
        return AnthropicProvider()
    if name in {"openai", "gpt", "chatgpt"}:
        return OpenAIProvider()
    raise LLMError(f"unknown LLM provider: {name}")
