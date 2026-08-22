"""
Prompt construction.

Two rules shape every prompt here:

* Ask for **java.util.regex**, not "regex". Spark evaluates patterns with
  ``java.util.regex.Pattern``; a model left to its own devices produces Python
  or PCRE flavour roughly a third of the time.
* Ask the model to commit to **its own test cases**. We then check the pattern
  against them (see :mod:`apps.llm.validation`), which turns a vague
  "does this look right?" into a pass/fail signal before any cluster time is
  spent.

Only the user's description plus a few sample values are sent -- never a column
of real data. The Gemini free tier may use prompts to improve Google's models,
so the payload is kept to the minimum that produces a good pattern.
"""
from __future__ import annotations

import json

from .spec import Operation, RegexRequest

SYSTEM_INSTRUCTION = """\
You translate a data analyst's plain-English description into a single regular \
expression for Apache Spark.

Hard requirements:
* The pattern MUST be valid java.util.regex syntax. Do NOT use Python-only \
constructs such as (?P<name>...), (?#comment), or POSIX classes like \
[[:alpha:]].
* Do NOT use nested quantifiers such as (a+)+ or (\\w+)* , and never place two \
unbounded wildcards next to each other (.*.*). These cause catastrophic \
backtracking and will be rejected.
* Prefer explicit character classes and bounded repetition over greedy \
wildcards.
* Do not anchor with ^ or $ unless the user asked for whole-cell matching.
* Escape backslashes correctly for JSON output.
* Return ONLY the JSON object described by the schema. No prose, no code fences.

You must also supply your own test cases: 3-6 realistic strings the pattern \
must match, and 3-6 near-miss strings it must not match. These are executed \
against your pattern, so be honest -- a pattern that fails its own examples is \
rejected.
"""

_OPERATION_GUIDANCE = {
    Operation.REPLACE: """\
Operation: REPLACE. The caller supplies a literal replacement value, so you only \
need the pattern that selects the text to be replaced. Ignore
`replacement_template` and `group`.""",
    Operation.MASK: """\
Operation: MASK. The match is rewritten through a java.util.regex replacement \
template, so the pattern MUST contain capture groups for the parts to keep, and \
`replacement_template` MUST reference them with $1, $2, ...

Example -- "mask emails but keep the first letter and the domain":
  pattern:              ([A-Za-z0-9])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\\.[A-Za-z]{2,7})
  replacement_template: $1***$2
Example -- "keep only the last 4 digits of a card number":
  pattern:              \\b\\d{4}[ -]?\\d{4}[ -]?\\d{4}[ -]?(\\d{4})\\b
  replacement_template: **** **** **** $1""",
    Operation.EXTRACT: """\
Operation: EXTRACT. The matched text is written into a NEW column. Put the part \
to keep in a capture group and set `group` to its index (use 0 for the whole \
match).

Example -- "extract the domain from an email address":
  pattern: [A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\\.[A-Za-z]{2,7})
  group:   1""",
    Operation.VALIDATE: """\
Operation: VALIDATE. The pattern describes what a VALID value looks like; rows \
whose value does not match are flagged. Anchor the pattern with ^ and $ so the \
whole cell must be valid. Ignore `replacement_template` and `group`.""",
}


def build_prompt(request: RegexRequest, repair_hint: str = "") -> str:
    """Assemble the user-turn prompt. ``repair_hint`` drives the retry pass."""
    payload: dict[str, object] = {
        "operation": request.operation,
        "user_description": request.description,
        "target_columns": request.columns[:20],
    }
    if request.sample_values:
        # A handful of values only: enough to disambiguate formats, small enough
        # to keep data exposure negligible.
        payload["sample_values_from_those_columns"] = request.sample_values[:5]
    if request.operation in Operation.NEEDS_REPLACEMENT and request.replacement_value:
        payload["literal_replacement_value"] = request.replacement_value

    sections = [
        _OPERATION_GUIDANCE[request.operation],
        "",
        "Request:",
        json.dumps(payload, ensure_ascii=False, indent=2),
    ]

    if repair_hint:
        sections += [
            "",
            "Your previous attempt was REJECTED by the validator:",
            f"  {repair_hint}",
            "Produce a corrected pattern that avoids that problem.",
        ]

    return "\n".join(sections)
