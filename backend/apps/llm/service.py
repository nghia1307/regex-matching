"""
Regex resolution service: prompt -> cache -> provider -> validate -> cache.

This is the only entry point the task layer uses. It owns three concerns the
brief calls out explicitly:

* **Caching** -- identical natural-language prompts are never re-sent to the
  LLM. The key is the *normalised* description plus operation plus model, so
  "Find Email Addresses" and "find email addresses" share one entry.
* **Validation** -- nothing reaches Spark without passing
  :func:`apps.llm.validation.validate_spec`.
* **Degradation** -- a rejected pattern triggers one repair round-trip that
  tells the model exactly what the validator disliked.
"""
from __future__ import annotations

import hashlib
import logging
import time

from django.conf import settings
from django.core.cache import cache

from .providers import LLMError, build_provider
from .spec import RegexRequest, RegexSpec
from .validation import RegexRejected, validate_spec

logger = logging.getLogger(__name__)

# Bump when the prompt or the validation rules change in a way that should
# invalidate previously cached patterns.
CACHE_VERSION = "v3"

_CACHE_PREFIX = "llm:regex"
_STAT_HIT = "llm:stats:cache_hits"
_STAT_MISS = "llm:stats:cache_misses"
_STAT_CALLS = "llm:stats:provider_calls"


class RegexResolutionError(RuntimeError):
    """Nothing usable could be produced for this description."""


def cache_key(request: RegexRequest, provider_name: str, model: str) -> str:
    digest = hashlib.sha256(
        "|".join(
            [
                CACHE_VERSION,
                provider_name,
                model,
                request.operation,
                request.normalised_description(),
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"{_CACHE_PREFIX}:{digest}"


def _bump(counter: str) -> None:
    """Best-effort metric; never let telemetry break a job."""
    try:
        cache.get_or_set(counter, 0, None)
        cache.incr(counter)
    except Exception:  # noqa: BLE001
        logger.debug("could not increment %s", counter, exc_info=True)


def cache_stats() -> dict[str, int]:
    def _read(key: str) -> int:
        try:
            return int(cache.get(key) or 0)
        except Exception:  # noqa: BLE001
            return 0

    hits, misses, calls = _read(_STAT_HIT), _read(_STAT_MISS), _read(_STAT_CALLS)
    total = hits + misses
    return {
        "cache_hits": hits,
        "cache_misses": misses,
        "provider_calls": calls,
        "hit_rate_percent": round(100.0 * hits / total, 1) if total else 0.0,
    }


def resolve_regex(
    request: RegexRequest, *, force_refresh: bool = False
) -> tuple[RegexSpec, bool]:
    """
    Return ``(spec, was_cached)`` for a natural-language description.

    Raises :class:`RegexResolutionError` if neither the configured provider nor
    the fallback can produce a pattern that survives validation.
    """
    if not request.description.strip():
        raise RegexResolutionError("describe the pattern you want to match")

    provider = build_provider()
    key = cache_key(request, provider.name, provider.model)

    if not force_refresh:
        cached = cache.get(key)
        if cached:
            _bump(_STAT_HIT)
            logger.info(
                "regex cache hit", extra={"key": key, "operation": request.operation}
            )
            return RegexSpec.from_dict(cached), True

    _bump(_STAT_MISS)
    spec = _generate_with_repair(provider, request)

    cache.set(key, spec.as_dict(), timeout=settings.LLM_CACHE_TTL_SECONDS)
    return spec, False


def _generate_with_repair(provider, request: RegexRequest) -> RegexSpec:
    """One generate attempt, then one validator-guided repair."""
    attempts = 2
    hint = ""
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            _bump(_STAT_CALLS)
            spec = provider.generate(request, repair_hint=hint)
            spec = validate_spec(spec)
        except RegexRejected as exc:
            last_error = exc
            hint = str(exc)
            logger.warning(
                "pattern rejected by validator (attempt %s/%s): %s",
                attempt,
                attempts,
                exc,
            )
            continue
        except LLMError as exc:
            last_error = exc
            logger.warning("provider %s failed: %s", provider.name, exc)
            break
        else:
            logger.info(
                "regex generated",
                extra={
                    "provider": provider.name,
                    "attempt": attempt,
                    "ms": round((time.perf_counter() - started) * 1000),
                    "self_test": spec.self_test_passed,
                },
            )
            return spec

    raise RegexResolutionError(str(last_error or "no pattern could be generated"))


_PROVIDER_HEALTH = {
    "gemini": lambda: (settings.GEMINI_MODEL, bool(settings.GEMINI_API_KEY)),
    "anthropic": lambda: (settings.ANTHROPIC_MODEL, bool(settings.ANTHROPIC_API_KEY)),
    "claude": lambda: (settings.ANTHROPIC_MODEL, bool(settings.ANTHROPIC_API_KEY)),
    "openai": lambda: (settings.OPENAI_MODEL, bool(settings.OPENAI_API_KEY)),
    "gpt": lambda: (settings.OPENAI_MODEL, bool(settings.OPENAI_API_KEY)),
    "chatgpt": lambda: (settings.OPENAI_MODEL, bool(settings.OPENAI_API_KEY)),
}


def provider_health() -> dict[str, object]:
    """Cheap, no-network description of the LLM configuration for /api/health."""
    configured = (settings.LLM_PROVIDER or "gemini").lower()
    model, api_key_present = _PROVIDER_HEALTH.get(configured, lambda: ("unknown", False))()
    return {
        "configured_provider": configured,
        "model": model,
        "api_key_present": api_key_present,
        "cache_ttl_seconds": settings.LLM_CACHE_TTL_SECONDS,
        **cache_stats(),
    }
