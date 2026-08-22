"""Provider selection and construction -- not the vendor SDKs themselves."""
from __future__ import annotations

import pytest

from apps.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    LLMError,
    OpenAIProvider,
    build_provider,
)


def test_build_provider_defaults_to_settings(settings):
    settings.LLM_PROVIDER = "gemini"
    assert isinstance(build_provider(), GeminiProvider)


@pytest.mark.parametrize("name", ["anthropic", "claude", "ANTHROPIC"])
def test_build_provider_resolves_anthropic_aliases(settings, name):
    settings.ANTHROPIC_API_KEY = "test-key"
    assert isinstance(build_provider(name), AnthropicProvider)


@pytest.mark.parametrize("name", ["openai", "gpt", "chatgpt", "OpenAI"])
def test_build_provider_resolves_openai_aliases(settings, name):
    settings.OPENAI_API_KEY = "test-key"
    assert isinstance(build_provider(name), OpenAIProvider)


def test_build_provider_rejects_unknown_name():
    with pytest.raises(LLMError, match="unknown LLM provider"):
        build_provider("not-a-real-provider")


def test_anthropic_provider_requires_an_api_key(settings):
    settings.ANTHROPIC_API_KEY = ""
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()


def test_openai_provider_requires_an_api_key(settings):
    settings.OPENAI_API_KEY = ""
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        OpenAIProvider()
