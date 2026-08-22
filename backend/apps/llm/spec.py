"""
The contract between the LLM and the Spark engine.

The LLM never returns code and is never ``eval``-ed. It returns a *structured
spec* drawn from a closed vocabulary of four operations, which the Spark layer
maps onto native column expressions. That boundary is what makes it safe to put
model output in front of a distributed job.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


class Operation:
    """Closed set of supported transformations."""

    REPLACE = "REPLACE"
    MASK = "MASK"
    EXTRACT = "EXTRACT"
    VALIDATE = "VALIDATE"

    ALL = (REPLACE, MASK, EXTRACT, VALIDATE)

    LABELS = {
        REPLACE: "Replace matches with a fixed value",
        MASK: "Partially mask matches, keeping the pieces you choose",
        EXTRACT: "Extract matches into a new column",
        VALIDATE: "Flag rows whose value does not match",
    }

    #: Which user-supplied field each operation consumes.
    NEEDS_REPLACEMENT = (REPLACE,)
    CREATES_COLUMN = (EXTRACT, VALIDATE)


@dataclass
class RegexRequest:
    """What we ask the model for."""

    description: str
    operation: str = Operation.REPLACE
    columns: list[str] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    replacement_value: str = ""

    def normalised_description(self) -> str:
        return " ".join(self.description.lower().split())


@dataclass
class RegexSpec:
    """What the model gives back, after validation."""

    pattern: str
    operation: str = Operation.REPLACE
    case_insensitive: bool = False
    explanation: str = ""
    confidence: float = 0.0
    should_match: list[str] = field(default_factory=list)
    should_not_match: list[str] = field(default_factory=list)
    # MASK: a java.util.regex replacement template, e.g. "$1***$3"
    replacement_template: str = ""
    # EXTRACT: which capture group to pull out (0 = whole match)
    group: int = 0
    provider: str = ""
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    self_test_passed: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegexSpec":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})

    @property
    def effective_pattern(self) -> str:
        """Inline the case-insensitive flag -- Spark takes flags in-pattern."""
        if self.case_insensitive and not self.pattern.startswith("(?i)"):
            return f"(?i){self.pattern}"
        return self.pattern


#: JSON schema handed to Gemini as a response schema, so we get parseable output
#: instead of prose wrapped in code fences.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "A java.util.regex pattern, unanchored unless the user asked for anchoring.",
        },
        "case_insensitive": {"type": "boolean"},
        "replacement_template": {
            "type": "string",
            "description": "For MASK only: replacement template using $1..$9 backreferences.",
        },
        "group": {
            "type": "integer",
            "description": "For EXTRACT only: capture group index to keep (0 = whole match).",
        },
        "confidence": {"type": "number"},
        "explanation": {"type": "string"},
        "should_match": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-6 example strings the pattern MUST match.",
        },
        "should_not_match": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-6 near-miss strings the pattern MUST NOT match.",
        },
    },
    "required": [
        "pattern",
        "case_insensitive",
        "confidence",
        "explanation",
        "should_match",
        "should_not_match",
    ],
}
