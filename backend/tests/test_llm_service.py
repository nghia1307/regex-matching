"""Caching and the validator-guided repair round-trip."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.llm.providers import LLMError
from apps.llm.service import RegexResolutionError, cache_key, resolve_regex
from apps.llm.spec import Operation, RegexRequest, RegexSpec
from tests.fake_llm import FakeLLMProvider


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def request_for(description: str, operation: str = Operation.REPLACE) -> RegexRequest:
    return RegexRequest(description=description, operation=operation, columns=["Email"])


def test_fake_provider_finds_emails():
    spec = FakeLLMProvider().generate(request_for("find email addresses"))
    assert "@" in spec.pattern
    assert spec.provider == "fake"


def test_identical_prompts_hit_the_cache(monkeypatch):
    """The brief's requirement: the same prompt must not be re-sent to the LLM."""
    calls: list[str] = []
    original = FakeLLMProvider.generate

    def counting(self, request, repair_hint=""):
        calls.append(request.description)
        return original(self, request, repair_hint)

    monkeypatch.setattr(FakeLLMProvider, "generate", counting)

    first, cached_first = resolve_regex(request_for("Find email addresses"))
    second, cached_second = resolve_regex(request_for("find   EMAIL   addresses"))

    assert cached_first is False
    assert cached_second is True, "normalised prompt should hit the cache"
    assert len(calls) == 1
    assert first.pattern == second.pattern


def test_force_refresh_bypasses_the_cache(monkeypatch):
    calls: list[str] = []
    original = FakeLLMProvider.generate

    def counting(self, request, repair_hint=""):
        calls.append(request.description)
        return original(self, request, repair_hint)

    monkeypatch.setattr(FakeLLMProvider, "generate", counting)

    resolve_regex(request_for("find email addresses"))
    resolve_regex(request_for("find email addresses"), force_refresh=True)
    assert len(calls) == 2


def test_cache_key_separates_operations():
    replace = cache_key(request_for("find emails", Operation.REPLACE), "p", "m")
    extract = cache_key(request_for("find emails", Operation.EXTRACT), "p", "m")
    assert replace != extract


def test_unresolvable_description_raises():
    with pytest.raises(RegexResolutionError):
        resolve_regex(request_for("do something clever with the vibes"))


def test_repair_round_trip_recovers_from_a_rejected_pattern(monkeypatch, settings):
    """
    First answer is catastrophic, second is fine. The validator's complaint must
    be fed back to the provider, and the good pattern must be what gets cached.
    """
    settings.LLM_PROVIDER = "gemini"
    settings.GEMINI_API_KEY = "test-key"

    attempts: list[str] = []

    class FlakyProvider:
        name = "gemini"
        model = "test-model"

        def generate(self, request, repair_hint=""):
            attempts.append(repair_hint)
            if len(attempts) == 1:
                return RegexSpec(pattern=r"(a+)+b", operation=request.operation)
            return RegexSpec(
                pattern=r"a+b",
                operation=request.operation,
                should_match=["aab"],
                should_not_match=["c"],
            )

    monkeypatch.setattr("apps.llm.service.build_provider", lambda name="": FlakyProvider())

    spec, cached = resolve_regex(request_for("find a's followed by b"))

    assert cached is False
    assert spec.pattern == r"a+b"
    assert attempts[0] == "", "first attempt gets no hint"
    assert "unsafe pattern" in attempts[1], "the repair attempt must be told what broke"


def test_a_dead_provider_surfaces_as_a_resolution_error(monkeypatch, settings):
    """There is no offline fallback: a dead provider must fail the job, not degrade silently."""

    class DeadProvider:
        name = "gemini"
        model = "test-model"

        def generate(self, request, repair_hint=""):
            raise LLMError("no quota")

    monkeypatch.setattr("apps.llm.service.build_provider", lambda name="": DeadProvider())

    with pytest.raises(RegexResolutionError, match="no quota"):
        resolve_regex(request_for("find email addresses"))


def test_truncated_response_is_reported_as_truncation_not_bad_json():
    """
    Reasoning tokens come out of the same output budget, so an over-tight cap
    truncates the JSON mid-string. That must be reported for what it is --
    otherwise the real cause is invisible behind a JSON parse error.
    """
    from apps.llm.providers import LLMTransientError
    from apps.llm.providers.gemini import _check_finish_reason

    class FakeResponse:
        candidates = [type("C", (), {"finish_reason": "FinishReason.MAX_TOKENS"})()]
        usage_metadata = type("U", (), {"total_token_count": 2419})()

    with pytest.raises(LLMTransientError, match="truncated"):
        _check_finish_reason(FakeResponse())


def test_safety_block_is_a_permanent_error():
    from apps.llm.providers import LLMError
    from apps.llm.providers.gemini import _check_finish_reason

    class Blocked:
        candidates = [type("C", (), {"finish_reason": "FinishReason.SAFETY"})()]
        usage_metadata = None

    with pytest.raises(LLMError, match="refused"):
        _check_finish_reason(Blocked())


def test_empty_candidate_list_is_an_error():
    from apps.llm.providers import LLMError
    from apps.llm.providers.gemini import _check_finish_reason

    with pytest.raises(LLMError, match="no candidates"):
        _check_finish_reason(type("R", (), {"candidates": []})())


def test_gemini_response_parsing_rejects_junk():
    from apps.llm.providers.base import parse_json_response

    with pytest.raises(LLMError):
        parse_json_response("")
    with pytest.raises(LLMError):
        parse_json_response("I think the answer is /foo/")
    # Fenced JSON is tolerated even though the schema should prevent it.
    assert parse_json_response('```json\n{"pattern": "a"}\n```') == {"pattern": "a"}
