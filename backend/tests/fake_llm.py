"""
Deterministic, offline stand-in for a real LLM provider, used only by tests.

Production always talks to Gemini -- there is no offline mode. This fixture
exists purely so the task/Spark layer can be exercised without a network call
or a Gemini API key; it must never be imported outside ``tests/``.
"""
from __future__ import annotations

import re

from apps.llm.providers import LLMError
from apps.llm.spec import Operation, RegexRequest, RegexSpec

_EMAIL = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"

_LIBRARY: tuple[tuple[tuple[str, ...], dict], ...] = (
    (
        ("email", "e-mail", "mail address"),
        {
            "pattern": _EMAIL,
            "explanation": "Standard email address: local part, @, domain, TLD of 2-7 letters.",
            "should_match": ["john.doe@example.com", "jane_smith@domain.co.uk"],
            "should_not_match": ["john.doe@", "@example.com", "not an email"],
            "mask": (
                r"([A-Za-z0-9])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,7})",
                "$1***$2",
            ),
            "extract": (r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,7})", 1),
            "validate": r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}$",
        },
    ),
    (
        ("phone", "telephone", "mobile number", "cell number"),
        {
            "pattern": r"\+?\d{1,3}[-.\s]?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}",
            "explanation": "International or local phone number with optional separators.",
            "should_match": ["+1 415-555-0132", "020 7946 0958", "0912.345.678"],
            "should_not_match": ["12", "no digits here"],
            "mask": (r"(\+?\d{1,3})[-.\s]?[\d\s().-]*(\d{3})\b", "$1*****$2"),
            "extract": (r"(\+?\d{1,3})[-.\s]?\(?\d{2,4}\)?", 1),
            "validate": r"^\+?[\d\s().-]{7,20}$",
        },
    ),
    (
        ("credit card", "card number", "pan"),
        {
            "pattern": r"\b(?:\d{4}[ -]?){3}\d{4}\b",
            "explanation": "16-digit card number in groups of four.",
            "should_match": ["4111 1111 1111 1111", "5500-0000-0000-0004"],
            "should_not_match": ["1234", "4111 1111"],
            "mask": (r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?(\d{4})\b", "**** **** **** $1"),
            "extract": (r"\b(?:\d{4}[ -]?){3}(\d{4})\b", 1),
            "validate": r"^(?:\d{4}[ -]?){3}\d{4}$",
        },
    ),
    (
        ("ssn", "social security"),
        {
            "pattern": r"\b\d{3}-\d{2}-\d{4}\b",
            "explanation": "US Social Security number in NNN-NN-NNNN form.",
            "should_match": ["123-45-6789"],
            "should_not_match": ["123456789", "12-345-678"],
            "mask": (r"\b\d{3}-\d{2}-(\d{4})\b", "***-**-$1"),
            "extract": (r"\b\d{3}-\d{2}-(\d{4})\b", 1),
            "validate": r"^\d{3}-\d{2}-\d{4}$",
        },
    ),
    (
        ("ip address", "ipv4", "ip "),
        {
            "pattern": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            "explanation": "Dotted-quad IPv4 address.",
            "should_match": ["192.168.0.1", "10.0.0.255"],
            "should_not_match": ["1.2.3", "abc.def.ghi.jkl"],
            "mask": (r"\b(\d{1,3}\.\d{1,3})\.\d{1,3}\.\d{1,3}\b", "$1.x.x"),
            "extract": (r"\b((?:\d{1,3}\.){3}\d{1,3})\b", 1),
            "validate": r"^(?:\d{1,3}\.){3}\d{1,3}$",
        },
    ),
    (
        ("url", "link", "website", "http"),
        {
            "pattern": r"https?://[^\s,;\"']+",
            "explanation": "HTTP or HTTPS URL up to the first whitespace or delimiter.",
            "should_match": ["https://example.com/a?b=1", "http://foo.io"],
            "should_not_match": ["example.com", "ftp://files"],
            "mask": (r"(https?://[^/\s]+)/[^\s]*", "$1/***"),
            "extract": (r"https?://([^/\s:]+)", 1),
            "validate": r"^https?://[^\s]+$",
        },
    ),
    (
        ("uuid", "guid"),
        {
            "pattern": r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
            "explanation": "Canonical 8-4-4-4-12 UUID.",
            "should_match": ["123e4567-e89b-12d3-a456-426614174000"],
            "should_not_match": ["123e4567", "not-a-uuid"],
            "validate": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        },
    ),
    (
        ("date", "birthday", "dob"),
        {
            "pattern": r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b",
            "explanation": "ISO (YYYY-MM-DD) or slash-separated dates.",
            "should_match": ["2026-08-22", "8/22/2026"],
            "should_not_match": ["22 August", "20260822"],
            "extract": (r"\b(\d{4})-\d{2}-\d{2}\b", 1),
            "validate": r"^\d{4}-\d{2}-\d{2}$",
        },
    ),
    (
        ("postcode", "postal code", "zip"),
        {
            "pattern": r"\b\d{5}(?:-\d{4})?\b",
            "explanation": "US ZIP or ZIP+4 code.",
            "should_match": ["94107", "94107-1234"],
            "should_not_match": ["9410", "ABCDE"],
            "validate": r"^\d{5}(?:-\d{4})?$",
        },
    ),
    (
        ("html tag", "markup", "html"),
        {
            "pattern": r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?>",
            "explanation": "HTML/XML opening or closing tag.",
            "should_match": ["<p>", "</div>", "<a href='x'>"],
            "should_not_match": ["3 < 5", "plain text"],
        },
    ),
    (
        ("whitespace", "extra spaces", "multiple spaces"),
        {
            "pattern": r"\s{2,}",
            "explanation": "Runs of two or more whitespace characters.",
            "should_match": ["a  b", "x\t\tz"],
            "should_not_match": ["a b"],
        },
    ),
    (
        ("digit", "number", "numeric"),
        {
            "pattern": r"\d+",
            "explanation": "One or more consecutive digits.",
            "should_match": ["42", "abc123"],
            "should_not_match": ["abc"],
            "validate": r"^\d+$",
        },
    ),
    (
        ("currency", "amount", "price", "money"),
        {
            "pattern": r"[$£€]\s?\d[\d,]*(?:\.\d{2})?",
            "explanation": "Currency amount with a leading symbol.",
            "should_match": ["$1,299.00", "£50"],
            "should_not_match": ["1299", "USD 20"],
        },
    ),
    (
        ("non-ascii", "special character", "emoji", "accent"),
        {
            "pattern": r"[^\x00-\x7F]+",
            "explanation": "Runs of characters outside the ASCII range.",
            "should_match": ["café", "naïve"],
            "should_not_match": ["plain ascii"],
        },
    ),
)


class FakeLLMProvider:
    """Deterministic, offline, no network -- test double for a real provider."""

    name = "fake"
    model = "test-fixture"

    def generate(self, request: RegexRequest, repair_hint: str = "") -> RegexSpec:
        text = request.normalised_description()

        entry = self._lookup(text)
        if entry is None:
            entry = self._from_literal(text)
        if entry is None:
            raise LLMError(f"the fake provider has no pattern for: {text!r}")

        spec = self._build(entry, request)
        spec.provider = self.name
        spec.model = self.model
        return spec

    # -- internals ---------------------------------------------------------- #
    def _lookup(self, text: str) -> dict | None:
        for keywords, entry in _LIBRARY:
            if any(keyword in text for keyword in keywords):
                return entry
        return None

    def _from_literal(self, text: str) -> dict | None:
        """Handle 'containing "foo"', 'starting with "foo"', 'ending with X'."""
        quoted = re.findall(r"['\"]([^'\"]{1,80})['\"]", text)
        if not quoted:
            return None
        literal = re.escape(quoted[0])
        if "start" in text or "begin" in text or "prefix" in text:
            pattern = f"^{literal}"
        elif "end" in text or "suffix" in text:
            pattern = f"{literal}$"
        else:
            pattern = literal
        return {
            "pattern": pattern,
            "explanation": f"Literal match for {quoted[0]!r} derived from the description.",
            "should_match": [quoted[0]],
            "should_not_match": [],
        }

    def _build(self, entry: dict, request: RegexRequest) -> RegexSpec:
        operation = request.operation
        pattern = entry["pattern"]
        template = ""
        group = 0

        if operation == Operation.MASK:
            if "mask" not in entry:
                raise LLMError("the fake provider has no masking rule for that pattern")
            pattern, template = entry["mask"]
        elif operation == Operation.EXTRACT:
            if "extract" in entry:
                pattern, group = entry["extract"]
            else:
                group = 0
        elif operation == Operation.VALIDATE:
            pattern = entry.get("validate", f"^{entry['pattern']}$")

        return RegexSpec(
            pattern=pattern,
            operation=operation,
            case_insensitive=False,
            explanation=entry["explanation"],
            confidence=0.55,
            should_match=list(entry.get("should_match", [])),
            should_not_match=list(entry.get("should_not_match", [])),
            replacement_template=template,
            group=group,
        )
