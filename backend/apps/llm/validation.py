"""
Regex validation: the gate between model output and a five-million-row job.

Four independent checks, cheapest first:

1. *Budget*      -- length cap, so nothing pathological is even compiled.
2. *Syntax*      -- must compile, and must not use Python-only constructs that
                    java.util.regex (what Spark uses) would reject. Python
                    named groups are rewritten rather than refused.
3. *Static ReDoS*-- known catastrophic shapes such as ``(a+)+`` or ``.*.*``.
4. *Dynamic*     -- the pattern actually runs against adversarial input inside a
                    hard-killed subprocess. Java's Matcher has no timeout, so a
                    pattern that backtracks here would hang an executor thread
                    with no way to interrupt it.

Then the spec self-tests: the model supplies its own positive and negative
examples and we check the pattern against them before spending cluster time.
"""
from __future__ import annotations

import logging
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from django.conf import settings

from .spec import Operation, RegexSpec

logger = logging.getLogger(__name__)


class RegexRejected(ValueError):
    """The pattern is unsafe or unusable; never reaches Spark."""


# --- 3. static catastrophic-backtracking shapes ----------------------------- #
_REDOS_SHAPES: tuple[tuple[str, str], ...] = (
    (r"\((?![?:])[^()]*[+*]\)\s*[+*]", "nested quantifier, e.g. (a+)+"),
    (r"\(\?:[^()]*[+*]\)\s*[+*]", "nested quantifier inside a non-capturing group"),
    (r"\.\*\.\*", "two adjacent .* -- exponential on failure"),
    (r"\.\+\.\+", "two adjacent .+ -- exponential on failure"),
    (r"\((?![?:])[^()]*\|[^()]*\)\s*[+*]", "quantified alternation, e.g. (a|a)*"),
    (r"\{\d{4,},?\d*\}", "repetition count in the thousands"),
)

# --- 2. constructs java.util.regex does not understand ---------------------- #
_JAVA_INCOMPATIBLE: tuple[tuple[str, str], ...] = (
    (r"\(\?#", "inline comments (?#...) are not supported by Java regex"),
    (r"\[\[:[a-z]+:\]\]", "POSIX classes like [[:alpha:]] -- use \\p{Alpha} instead"),
    (r"\(\?P>", "recursive group references are not supported by Java regex"),
)

_ADVERSARIAL_INPUTS = (
    "a" * 64 + "!",
    "0" * 64 + "!",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ("a1@._-" * 12) + "!",
    " " * 64 + "x",
    "<" + "x" * 60,
    "https://" + "a" * 56,
)


@dataclass
class ValidationResult:
    #: Canonical Java form -- this is what Spark executes.
    pattern: str
    #: Equivalent Python form, used for local compilation and probing.
    python_pattern: str = ""
    warnings: list[str] = field(default_factory=list)
    self_test_passed: bool | None = None
    duration_ms: float = 0.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def normalise_for_java(pattern: str) -> tuple[str, list[str]]:
    """
    Rewrite Python-flavoured constructs into their Java equivalents.

    Models frequently emit ``(?P<name>...)`` because most regex examples online
    are Python. Java spells the same thing ``(?<name>...)``. Rewriting is safer
    than refusing: the intent is unambiguous.
    """
    warnings: list[str] = []
    out = pattern
    if "(?P<" in out:
        out = out.replace("(?P<", "(?<")
        warnings.append("rewrote Python named groups (?P<n>) to Java syntax (?<n>)")
    if "(?P=" in out:
        out = re.sub(r"\(\?P=(\w+)\)", r"\\k<\1>", out)
        warnings.append("rewrote Python group back-references (?P=n) to \\k<n>")
    return out, warnings


def java_to_python(pattern: str) -> str:
    """
    Translate a Java pattern into the equivalent Python one.

    The canonical pattern we store and hand to Spark is Java-flavoured, but every
    local check -- compiling, the ReDoS probe, the self-test -- runs on Python's
    ``re``, and the two spell named groups differently. Lookbehinds
    (``(?<=``, ``(?<!``) are left alone: the group name pattern requires a
    letter after ``<``, so they cannot match.
    """
    out = re.sub(r"\(\?<([A-Za-z][A-Za-z0-9]*)>", r"(?P<\1>", pattern)
    return re.sub(r"\\k<([A-Za-z][A-Za-z0-9]*)>", r"(?P=\1)", out)


#: The probe body, run in a fresh interpreter. Kept tiny and dependency-free so
#: startup stays around 30 ms.
_PROBE_SOURCE = """
import json, re, sys
payload = json.load(sys.stdin)
try:
    compiled = re.compile(payload["pattern"])
    for probe in payload["inputs"]:
        compiled.search(probe)
    print(json.dumps({"status": "ok", "detail": ""}))
except Exception as exc:
    print(json.dumps({"status": "error", "detail": f"{type(exc).__name__}: {exc}"}))
"""


def _dynamic_backtracking_check(pattern: str, timeout_ms: int) -> None:
    """
    Run the pattern against adversarial inputs in a process we can hard-kill.

    ``subprocess`` rather than ``multiprocessing``, for two reasons that both
    bite in production:

    * Celery's prefork children are **daemonic**, and a daemonic process is not
      allowed to create ``multiprocessing`` children at all -- it raises
      ``AssertionError: daemonic processes are not allowed to have children``.
      ``subprocess`` carries no such restriction.
    * A fresh interpreter shares no state with a driver that has a JVM attached,
      so there is nothing to corrupt if the probe dies badly.

    The cost (one interpreter start) is paid once per *uncached* pattern.
    """
    payload = json.dumps({"pattern": pattern, "inputs": list(_ADVERSARIAL_INPUTS)})
    budget = max(timeout_ms / 1000.0, 0.25) + 1.0  # + interpreter startup

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-I", "-c", _PROBE_SOURCE],
            input=payload,
            capture_output=True,
            text=True,
            timeout=budget,
        )
    except subprocess.TimeoutExpired as exc:
        raise RegexRejected(
            "pattern did not finish against adversarial input within "
            f"{timeout_ms} ms -- rejected as a catastrophic-backtracking risk"
        ) from exc
    except OSError as exc:  # pragma: no cover - cannot start an interpreter
        logger.error("regex safety probe could not start: %s", exc)
        raise RegexRejected("could not run the regex safety probe") from exc

    try:
        verdict = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        verdict = {}

    status = verdict.get("status")
    if status == "ok":
        return
    if status == "error":
        raise RegexRejected(f"pattern failed to run: {verdict.get('detail', '')}")
    raise RegexRejected(
        "regex safety probe returned no verdict "
        f"(exit {completed.returncode}): {(completed.stderr or '')[:200]}"
    )


def validate_pattern(pattern: str, *, run_dynamic_check: bool = True) -> ValidationResult:
    """Raise :class:`RegexRejected` unless ``pattern`` is safe to hand to Spark."""
    started = time.perf_counter()

    if not pattern or not pattern.strip():
        raise RegexRejected("empty pattern")

    max_len = getattr(settings, "REGEX_MAX_LENGTH", 2000)
    if len(pattern) > max_len:
        raise RegexRejected(f"pattern is {len(pattern)} chars, limit is {max_len}")

    pattern, warnings = normalise_for_java(pattern)

    for probe, message in _JAVA_INCOMPATIBLE:
        if re.search(probe, pattern):
            raise RegexRejected(message)

    python_pattern = java_to_python(pattern)
    try:
        re.compile(python_pattern)
    except re.error as exc:
        raise RegexRejected(f"invalid regular expression: {exc}") from exc

    for probe, message in _REDOS_SHAPES:
        if re.search(probe, pattern):
            raise RegexRejected(f"unsafe pattern: {message}")

    if run_dynamic_check:
        _dynamic_backtracking_check(
            python_pattern, getattr(settings, "REGEX_TIMEOUT_MS", 1000)
        )

    return ValidationResult(
        pattern=pattern,
        python_pattern=python_pattern,
        warnings=warnings,
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )


def validate_template(template: str, pattern: str) -> list[str]:
    """MASK templates may only reference groups the pattern actually defines."""
    warnings: list[str] = []
    if not template:
        raise RegexRejected("MASK requires a replacement template such as '$1***'")
    try:
        group_count = re.compile(java_to_python(pattern)).groups
    except re.error as exc:  # pragma: no cover - pattern already compiled upstream
        raise RegexRejected(str(exc)) from exc

    referenced = {int(m) for m in re.findall(r"(?<!\\)\$(\d)", template)}
    bad = {g for g in referenced if g > group_count}
    if bad:
        raise RegexRejected(
            f"template references group(s) {sorted(bad)} but the pattern defines "
            f"{group_count}"
        )
    if not referenced:
        warnings.append(
            "MASK template has no $n back-reference, so matches are fully replaced"
        )
    return warnings


def self_test(spec: RegexSpec) -> tuple[bool | None, list[str]]:
    """
    Check the model's own examples against its own pattern.

    A pattern that fails the examples the model itself proposed is almost always
    wrong, and catching that here costs milliseconds instead of a cluster job.
    """
    if not spec.should_match and not spec.should_not_match:
        return None, []

    warnings: list[str] = []
    compiled = re.compile(java_to_python(spec.effective_pattern))

    missed = [s for s in spec.should_match if not compiled.search(s)]
    false_hits = [s for s in spec.should_not_match if compiled.search(s)]

    if missed:
        warnings.append(f"pattern does not match its own examples: {missed[:3]}")
    if false_hits:
        warnings.append(f"pattern matches strings it should not: {false_hits[:3]}")

    return (not missed and not false_hits), warnings


def validate_spec(spec: RegexSpec, *, run_dynamic_check: bool = True) -> RegexSpec:
    """Full validation pass; returns a cleaned copy of the spec."""
    result = validate_pattern(spec.pattern, run_dynamic_check=run_dynamic_check)
    spec.pattern = result.pattern
    spec.warnings = list(dict.fromkeys([*spec.warnings, *result.warnings]))

    if spec.operation == Operation.MASK:
        spec.warnings += validate_template(spec.replacement_template, spec.pattern)
    if spec.operation == Operation.EXTRACT:
        group_count = re.compile(java_to_python(spec.pattern)).groups
        if spec.group > group_count:
            raise RegexRejected(
                f"EXTRACT asks for group {spec.group} but the pattern defines "
                f"{group_count}"
            )

    passed, test_warnings = self_test(spec)
    spec.self_test_passed = passed
    spec.warnings = list(dict.fromkeys([*spec.warnings, *test_warnings]))
    return spec
