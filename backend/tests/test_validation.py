"""The safety gate: nothing unsafe should ever reach a Spark executor."""
from __future__ import annotations

import multiprocessing

import pytest

from apps.llm.spec import Operation, RegexSpec
from apps.llm.validation import (
    RegexRejected,
    normalise_for_java,
    self_test,
    validate_pattern,
    validate_spec,
    validate_template,
)

EMAIL = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"


def test_accepts_a_normal_pattern():
    result = validate_pattern(EMAIL)
    assert result.pattern == EMAIL


def test_rejects_invalid_syntax():
    with pytest.raises(RegexRejected, match="invalid regular expression"):
        validate_pattern(r"[a-z")


def test_rejects_empty_pattern():
    with pytest.raises(RegexRejected):
        validate_pattern("   ")


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+$",           # classic nested quantifier
        r"(?:x+)+y",         # same, non-capturing
        r".*.*=.*",          # adjacent unbounded wildcards
        r"(a|a)*b",          # quantified overlapping alternation
        r"a{5000}",          # absurd repetition
    ],
)
def test_rejects_catastrophic_shapes(pattern):
    with pytest.raises(RegexRejected, match="unsafe pattern"):
        validate_pattern(pattern, run_dynamic_check=False)


def test_rejects_patterns_over_the_length_limit(settings):
    settings.REGEX_MAX_LENGTH = 20
    with pytest.raises(RegexRejected, match="limit is 20"):
        validate_pattern("a" * 21)


def test_rejects_java_incompatible_constructs():
    with pytest.raises(RegexRejected, match="POSIX"):
        validate_pattern(r"[[:alpha:]]+", run_dynamic_check=False)
    with pytest.raises(RegexRejected, match="inline comments"):
        validate_pattern(r"foo(?#note)bar", run_dynamic_check=False)


def test_rewrites_python_named_groups_to_java_syntax():
    """Models emit Python flavour constantly; rewriting beats refusing."""
    pattern, warnings = normalise_for_java(r"(?P<user>\w+)@(?P<host>\w+)")
    assert pattern == r"(?<user>\w+)@(?<host>\w+)"
    assert warnings and "Java syntax" in warnings[0]

    result = validate_pattern(r"(?P<user>[a-z]+)@example\.com")
    assert "(?<user>" in result.pattern


def test_dynamic_check_kills_a_backtracking_pattern():
    """
    A pattern that slips past the static shapes must still be caught by actually
    running it. Java's Matcher has no timeout, so this is the last line of
    defence before an executor thread hangs.

    ``^(\\w+\\s?)*$`` is the textbook case: the quantified group ends in ``?``
    rather than ``+``/``*``, so the nested-quantifier shape check does not fire,
    yet a long non-matching string makes it explode exponentially.
    """
    with pytest.raises(RegexRejected, match="catastrophic|did not finish"):
        validate_pattern(r"^(\w+\s?)*$")


def _validate_in_child(queue) -> None:  # pragma: no cover - runs in a child
    """Target for the daemonic-process regression test below."""
    try:
        validate_pattern(EMAIL)
        queue.put(("ok", ""))
    except BaseException as exc:  # noqa: BLE001 - the failure mode IS the test
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def test_validation_works_inside_a_daemonic_process():
    """
    Celery's prefork children are daemonic, and a daemonic process may not create
    ``multiprocessing`` children at all -- so a probe built on multiprocessing
    dies with "daemonic processes are not allowed to have children" only once it
    reaches a real worker. The probe therefore uses ``subprocess``; this test is
    what keeps it that way.
    """
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    child = ctx.Process(target=_validate_in_child, args=(queue,), daemon=True)
    child.start()
    child.join(timeout=90)

    assert not child.is_alive(), "validation hung inside a daemonic process"
    status, detail = queue.get(timeout=10)
    assert status == "ok", detail


def test_static_shapes_are_checked_on_the_java_form_not_the_python_one():
    """A Python-flavoured pattern is normalised first, then judged."""
    result = validate_pattern(r"(?P<local>[a-z]+)@(?P<host>[a-z.]+)")
    assert result.pattern.startswith("(?<local>")
    assert result.python_pattern.startswith("(?P<local>")


def test_self_test_flags_a_pattern_that_fails_its_own_examples():
    spec = RegexSpec(
        pattern=r"^\d{3}$",
        should_match=["123", "abc"],
        should_not_match=["999"],
    )
    passed, warnings = self_test(spec)
    assert passed is False
    assert len(warnings) == 2  # a miss and a false hit


def test_self_test_passes_for_a_good_pattern():
    spec = RegexSpec(
        pattern=EMAIL,
        should_match=["a@b.com", "john.doe@example.co.uk"],
        should_not_match=["nope", "a@b"],
    )
    passed, warnings = self_test(spec)
    assert passed is True
    assert warnings == []


def test_mask_template_must_reference_real_groups():
    with pytest.raises(RegexRejected, match="group"):
        validate_template("$1***$3", r"(a)(b)")
    assert validate_template("$1***$2", r"(a)(b)") == []
    # No back-reference is legal but worth a warning.
    assert validate_template("***", r"(a)") != []


def test_extract_group_must_exist():
    spec = RegexSpec(pattern=r"(\w+)@(\w+)", operation=Operation.EXTRACT, group=5)
    with pytest.raises(RegexRejected, match="group 5"):
        validate_spec(spec, run_dynamic_check=False)


def test_case_insensitive_flag_is_inlined_for_spark():
    spec = RegexSpec(pattern="abc", case_insensitive=True)
    assert spec.effective_pattern == "(?i)abc"
