"""Secret and PII redaction for prompt/completion content.

Why this belongs in a normalisation layer
-----------------------------------------
Agent frameworks capture prompts and completions as span attributes, and one of
them -- OpenLLMetry -- defaults content capture to **on**, the opposite of the
OTel default. Prompts routinely contain whatever the user pasted: an AWS key, a
bearer token, a customer record. Once that reaches the telemetry store it is
replicated, retained under an observability retention policy nobody reviewed for
compliance, and readable by everyone with dashboard access.

Redacting at query time is too late; the secret is already at rest. Rosetta
redacts in the span pipeline, before export.

Detection is conservative on purpose. A false positive silently destroys the
content an engineer needs to debug an agent, so every pattern here is either
structurally unambiguous (an AWS key ID has a fixed prefix and length) or
validated (card numbers must pass Luhn). Loose patterns -- "any long hex string",
bare 9-digit numbers -- are deliberately absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Pattern

from .semconv import Rosetta, all_content_keys


@dataclass(frozen=True)
class Rule:
    """One detector."""

    kind: str
    pattern: Pattern[str]
    #: Severity if found. Credentials are errors; PII is a warning.
    severity: str = "error"
    #: Optional extra validation to suppress structurally-plausible noise.
    validator: str | None = None
    description: str = ""


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum. Without it, any 16-digit number reads as a card."""
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    total = 0
    parity = len(nums) % 2
    for index, digit in enumerate(nums):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


#: Ordered so the most specific credential patterns match first.
RULES: tuple[Rule, ...] = (
    Rule(
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"),
        description="AWS key IDs have a fixed prefix and length -- unambiguous.",
    ),
    Rule(
        "aws_secret_access_key",
        re.compile(
            r"(?i)\baws_?secret_?access_?key\b\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})[\"']?"
        ),
        description="Requires the labelled context; a bare 40-char blob is too loose.",
    ),
    Rule(
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
        description="PEM header. Zero false positives.",
    ),
    Rule(
        "openai_api_key",
        re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    Rule(
        "anthropic_api_key",
        re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    Rule(
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    ),
    Rule(
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    Rule(
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        description="Two base64 segments each starting with the '{\"' prefix, plus a signature.",
    ),
    Rule(
        "bearer_token",
        re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[\"']?bearer\s+([A-Za-z0-9._~+/-]{20,})"),
    ),
    Rule(
        "db_connection_string",
        re.compile(
            r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)://"
            r"[^\s:@/]+:[^\s:@/]+@[^\s/]+"
        ),
        description="Only matches when credentials are actually embedded.",
    ),
    Rule(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        severity="warning",
    ),
    Rule(
        "credit_card",
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        severity="warning",
        validator="luhn",
        description="Luhn-validated; a raw 16-digit regex alone is unusable.",
    ),
)

#: Kinds treated as credentials rather than personal data.
CREDENTIAL_KINDS: frozenset[str] = frozenset(
    {r.kind for r in RULES if r.severity == "error"}
)


@dataclass
class RedactionResult:
    """What redaction found and did on one span."""

    #: attribute key -> redacted value, for keys that changed.
    changed: dict[str, str] = field(default_factory=dict)
    #: kind -> number of occurrences.
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def kinds(self) -> list[str]:
        return sorted(self.counts)

    @property
    def has_credentials(self) -> bool:
        return any(k in CREDENTIAL_KINDS for k in self.counts)


def _placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def scan(text: str) -> dict[str, int]:
    """Count secrets in a string without modifying it."""
    counts: dict[str, int] = {}
    for rule in RULES:
        for match in rule.pattern.finditer(text):
            if rule.validator == "luhn" and not _luhn_ok(match.group(0)):
                continue
            counts[rule.kind] = counts.get(rule.kind, 0) + 1
    return counts


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Replace every detected secret with a typed placeholder.

    The placeholder keeps the *kind*, so a trace still tells you "an AWS key was
    pasted here" -- which is the security-relevant fact -- without storing it.
    """
    counts: dict[str, int] = {}
    result = text
    for rule in RULES:
        def _sub(match: re.Match[str]) -> str:
            if rule.validator == "luhn" and not _luhn_ok(match.group(0)):
                return match.group(0)
            counts[rule.kind] = counts.get(rule.kind, 0) + 1
            return _placeholder(rule.kind)

        result = rule.pattern.sub(_sub, result)
    return result, counts


def redact_attributes(
    attrs: dict[str, Any],
    *,
    extra_keys: Iterable[str] = (),
) -> RedactionResult:
    """Redact every known content-bearing attribute in place.

    Only content keys are scanned. Running every regex over every attribute of
    every span would be wasteful and would mangle unrelated fields.
    """
    result = RedactionResult()
    targets = set(all_content_keys()) | set(extra_keys)

    for key in targets:
        value = attrs.get(key)
        if not isinstance(value, str) or not value:
            continue
        cleaned, counts = redact_text(value)
        if counts:
            attrs[key] = cleaned
            result.changed[key] = cleaned
            for kind, n in counts.items():
                result.counts[kind] = result.counts.get(kind, 0) + n

    if result.counts:
        attrs[Rosetta.REDACTED.value] = True
        attrs[Rosetta.REDACTED_KINDS.value] = ",".join(result.kinds)
        attrs[Rosetta.REDACTED_COUNT.value] = result.total

    return result
