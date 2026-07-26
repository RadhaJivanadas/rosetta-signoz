"""The normalisation engine: many dialects in, one canonical vocabulary out.

The contract is deliberately narrow so the result is safe to run in a production
span pipeline:

* **Additive.** Source attributes are never removed or rewritten. Existing
  dashboards keep working; Rosetta only adds canonical keys.
* **Deterministic.** First match over an explicit precedence list. No heuristics,
  no model calls, no network.
* **Explainable.** Every canonical value records the source key it came from, so
  a disagreement can be traced back rather than argued about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping

from .semconv import (
    AGGREGATE_SOURCE_KEYS,
    DIALECTS,
    HANDOFF_KEYS,
    KNOWN_OPERATIONS,
    Canon,
    Dialect,
    Rosetta,
    detect_dialects,
    infer_operation,
    is_genai_span,
)

# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

#: Severity ordering used for scoring and for alert routing.
SEVERITIES: tuple[str, ...] = ("info", "warning", "error")


@dataclass(frozen=True)
class Finding:
    """One conformance problem detected on a span."""

    code: str
    severity: str
    message: str
    #: Attribute the finding is about, when there is a single one.
    attribute: str | None = None

    def __str__(self) -> str:
        where = f" [{self.attribute}]" if self.attribute else ""
        return f"{self.code} {self.severity.upper()}: {self.message}{where}"


# Weight subtracted from a 100-point conformance score, per finding.
_SEVERITY_WEIGHT: Mapping[str, int] = {"info": 2, "warning": 8, "error": 20}


class Code:
    """Stable finding codes. Referenced by dashboards, alerts and the README."""

    DARK_COST = "R001"          # GenAI span carrying no token usage at all
    LEGACY_NAME = "R002"        # resolved via a superseded spec name
    VENDOR_NAME = "R003"        # resolved via a vendor-proprietary namespace
    JSON_ENCODED = "R004"       # numeric value trapped inside a JSON string
    DUAL_EMISSION = "R005"      # same concept under two spellings at once
    MISSING_OPERATION = "R006"  # no gen_ai.operation.name, not inferable
    UNKNOWN_OPERATION = "R007"  # operation name not in the spec's value set
    MISSING_MODEL = "R008"      # no model -> cannot price, cannot group
    AGGREGATE_USAGE = "R009"    # roll-up usage; must be excluded from SUMs
    UNKNOWN_SERVICE = "R010"    # service.name missing or unknown_service
    BROKEN_HANDOFF = "R011"     # handoff target named but never arrived
    CONFLICTING_USAGE = "R014"  # same quantity reported twice, with different values


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class Normalized:
    """Outcome of normalising one span."""

    #: Canonical attributes to add to the span. Never includes source keys.
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Dialect names detected, in precedence order.
    dialects: tuple[str, ...] = ()
    #: canonical key -> source key it was resolved from.
    provenance: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    is_genai: bool = False
    #: True when token counts came from a roll-up attribute. Such spans must be
    #: excluded from SUM aggregations or every token is counted twice.
    is_aggregate: bool = False

    @property
    def score(self) -> int:
        """Conformance score in 0..100."""
        penalty = sum(_SEVERITY_WEIGHT.get(f.severity, 0) for f in self.findings)
        return max(0, 100 - penalty)

    def issue_codes(self) -> list[str]:
        # Stable order, de-duplicated -- this string lands on the span.
        seen: dict[str, None] = {}
        for f in self.findings:
            seen.setdefault(f.code, None)
        return list(seen)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_int(value: Any) -> int | None:
    """Token counts arrive as int, float or string depending on the emitter."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, AttributeError):
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _load_json_obj(raw: Any) -> Mapping[str, Any] | None:
    """Parse a JSON-encoded attribute, tolerating already-parsed dicts."""
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


#: Source keys that are superseded spec spellings rather than vendor inventions.
_LEGACY_KEYS: frozenset[str] = frozenset(
    {
        "gen_ai.system",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.usage.completion_tokens",
        "gen_ai.prompt",
        "gen_ai.completion",
    }
)


def _classify_source(key: str, dialect: Dialect) -> str | None:
    """Which finding code, if any, applies to resolving a value from ``key``."""
    if key in _LEGACY_KEYS:
        return Code.LEGACY_NAME
    if key.startswith("gen_ai."):
        return None  # canonical or canonical-adjacent
    return Code.VENDOR_NAME


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

#: Canonical keys whose values are token counts (coerced to int).
_INT_KEYS: frozenset[Canon] = frozenset(
    {
        Canon.INPUT_TOKENS,
        Canon.OUTPUT_TOKENS,
        Canon.REASONING_TOKENS,
        Canon.CACHE_READ_TOKENS,
        Canon.CACHE_WRITE_TOKENS,
    }
)


def _resolve(
    canon: Canon,
    attrs: Mapping[str, Any],
    dialects: tuple[Dialect, ...],
) -> tuple[Any, str, Dialect, bool] | None:
    """First non-empty value for ``canon``, honouring dialect precedence.

    Returns ``(value, source_key, dialect, from_json)`` or ``None``.
    Plain aliases across *all* dialects are tried before any JSON extraction, so
    a real numeric attribute always beats a value trapped in a JSON blob.
    """
    for dialect in dialects:
        for source_key in dialect.aliases.get(canon, ()):
            if source_key in attrs:
                value = attrs[source_key]
                if value is not None and value != "":
                    return value, source_key, dialect, False

    for dialect in dialects:
        for source_key, json_key in dialect.json_aliases.get(canon, ()):
            blob = _load_json_obj(attrs.get(source_key))
            if blob is None:
                continue
            value = blob.get(json_key)
            if value is not None and value != "":
                return value, f"{source_key}::{json_key}", dialect, True

    return None


def _collect_sources(
    canon: Canon,
    attrs: Mapping[str, Any],
    dialects: tuple[Dialect, ...],
) -> list[tuple[str, Any]]:
    """Every present source key for ``canon``, across all dialects.

    Used to detect the same quantity being reported twice. This is not
    hypothetical: AWS Strands emits ``gen_ai.usage.prompt_tokens`` *and*
    ``gen_ai.usage.input_tokens`` on the same span while migrating. Resolution
    by precedence silently picks one, which is the correct behaviour -- but
    staying silent about the duplicate is not, because anyone hand-writing a
    query that adds both is double-counting and has no way to know.
    """
    found: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for dialect in dialects:
        for source_key in dialect.aliases.get(canon, ()):
            if source_key in seen or source_key not in attrs:
                continue
            seen.add(source_key)
            value = attrs[source_key]
            if value is not None and value != "":
                found.append((source_key, value))
    return found


def normalize(
    attrs: Mapping[str, Any],
    *,
    resource_attrs: Mapping[str, Any] | None = None,
) -> Normalized:
    """Normalise one span's attributes into the canonical vocabulary.

    ``resource_attrs`` is optional and only consulted for ``service.name``, whose
    absence is a conformance finding in its own right -- unattributable spend is
    the practical consequence.
    """
    result = Normalized()
    result.is_genai = is_genai_span(attrs)
    if not result.is_genai:
        return result

    detected = detect_dialects(attrs)
    result.dialects = tuple(d.name for d in detected)
    # Fall back to full precedence order when no marker matched but the span
    # still looks like GenAI -- a bare gen_ai.request.model, for instance.
    search_order: tuple[Dialect, ...] = detected or DIALECTS

    # -- resolve every canonical key -------------------------------------
    for canon in Canon:
        found = _resolve(canon, attrs, search_order)
        if found is None:
            continue
        raw, source_key, dialect, from_json = found

        value: Any = raw
        if canon in _INT_KEYS:
            coerced = _coerce_int(raw)
            if coerced is None:
                continue
            value = coerced

        result.attributes[canon.value] = value
        result.provenance[canon.value] = source_key

        base_key = source_key.split("::", 1)[0]
        if from_json:
            result.findings.append(
                Finding(
                    Code.JSON_ENCODED,
                    "error",
                    f"{canon.value} was trapped inside JSON string "
                    f"{base_key!r}; unqueryable as a number until unpacked",
                    attribute=base_key,
                )
            )
        elif base_key in AGGREGATE_SOURCE_KEYS:
            result.is_aggregate = True
        else:
            code = _classify_source(base_key, dialect)
            if code == Code.LEGACY_NAME:
                result.findings.append(
                    Finding(
                        code,
                        "warning",
                        f"{canon.value} resolved from superseded name {base_key!r}",
                        attribute=base_key,
                    )
                )
            elif code == Code.VENDOR_NAME:
                result.findings.append(
                    Finding(
                        code,
                        "warning",
                        f"{canon.value} resolved from vendor attribute {base_key!r} "
                        f"({dialect.name}); not portable across backends",
                        attribute=base_key,
                    )
                )

    # -- duplicate reporting of the same quantity --------------------------
    # Checked only for token counts, where a duplicate turns directly into a
    # wrong bill.
    for canon in (Canon.INPUT_TOKENS, Canon.OUTPUT_TOKENS):
        sources = _collect_sources(canon, attrs, DIALECTS)
        if len(sources) < 2:
            continue
        values = {_coerce_int(v) for _, v in sources}
        values.discard(None)
        keys = ", ".join(k for k, _ in sources)
        if len(values) > 1:
            result.findings.append(
                Finding(
                    Code.CONFLICTING_USAGE,
                    "error",
                    f"{canon.value} reported by multiple attributes with "
                    f"DIFFERENT values ({keys}); resolved by precedence, but the "
                    "emitter is inconsistent",
                    attribute=keys,
                )
            )
        else:
            result.findings.append(
                Finding(
                    Code.CONFLICTING_USAGE,
                    "warning",
                    f"{canon.value} reported twice under different names "
                    f"({keys}); counted once here, but a query summing both "
                    "would double-count",
                    attribute=keys,
                )
            )

    # -- aggregate roll-up ------------------------------------------------
    if any(k in attrs for k in AGGREGATE_SOURCE_KEYS):
        # Present at all is worth flagging: even if the span had its own usage,
        # a naive SUM over the trace would double-count.
        result.is_aggregate = result.is_aggregate or not any(
            result.provenance.get(c.value, "") not in AGGREGATE_SOURCE_KEYS
            for c in (Canon.INPUT_TOKENS, Canon.OUTPUT_TOKENS)
            if c.value in result.provenance
        )
        result.findings.append(
            Finding(
                Code.AGGREGATE_USAGE,
                "info",
                "span carries gen_ai.aggregated_usage.* roll-up attributes; "
                "exclude from SUM aggregations to avoid double-counting",
            )
        )

    # -- operation --------------------------------------------------------
    operation = infer_operation(attrs) or result.attributes.get(Canon.OPERATION.value)
    if operation:
        result.attributes[Canon.OPERATION.value] = operation
        if operation not in KNOWN_OPERATIONS:
            result.findings.append(
                Finding(
                    Code.UNKNOWN_OPERATION,
                    "info",
                    f"operation {operation!r} is not one of the spec's values",
                    attribute=Canon.OPERATION.value,
                )
            )
    else:
        result.findings.append(
            Finding(
                Code.MISSING_OPERATION,
                "warning",
                "no gen_ai.operation.name and none inferable from span kind",
            )
        )

    # -- dual emission ----------------------------------------------------
    if "gen_ai.system" in attrs and Canon.PROVIDER.value in attrs:
        result.findings.append(
            Finding(
                Code.DUAL_EMISSION,
                "info",
                "both gen_ai.system and gen_ai.provider.name present "
                "(mid-migration); coalesced by precedence, never summed",
                attribute="gen_ai.system",
            )
        )

    # -- model ------------------------------------------------------------
    model = result.attributes.get(Canon.REQUEST_MODEL.value) or result.attributes.get(
        Canon.RESPONSE_MODEL.value
    )
    if not model:
        result.findings.append(
            Finding(
                Code.MISSING_MODEL,
                "warning",
                "no model attribute; span cannot be priced or grouped by model",
            )
        )

    # -- dark cost --------------------------------------------------------
    # The finding that pays for the whole project: an LLM call whose token usage
    # never made it into telemetry is spend that no dashboard can ever show.
    #
    # Container spans are excluded. An `invoke_agent` or `execute_tool` span
    # carries no token usage *by design* -- its children do. Flagging them
    # produced roughly one false positive per agent run, which would have made
    # the signal worthless precisely where it matters most.
    inference_ops = {"chat", "generate_content", "embeddings"}
    container_ops = {
        "invoke_agent",
        "create_agent",
        "invoke_workflow",
        "plan",
        "execute_tool",
        "retrieve",
        "guardrail",
        "evaluate",
    }
    has_usage = any(
        result.attributes.get(k.value) for k in (Canon.INPUT_TOKENS, Canon.OUTPUT_TOKENS)
    )
    looks_like_inference = operation in inference_ops or (
        model is not None and operation not in container_ops
    )
    if not has_usage and looks_like_inference:
        result.findings.append(
            Finding(
                Code.DARK_COST,
                "error",
                "GenAI span reports no token usage in any known dialect; "
                "this is spend that is invisible to every cost dashboard",
            )
        )
        result.attributes[Rosetta.DARK_COST.value] = True

    # -- handoffs ---------------------------------------------------------
    for key in HANDOFF_KEYS:
        if key in attrs:
            result.attributes[key] = attrs[key]

    # -- service attribution ----------------------------------------------
    service = (resource_attrs or {}).get("service.name")
    if not service or service == "unknown_service":
        result.findings.append(
            Finding(
                Code.UNKNOWN_SERVICE,
                "warning",
                "service.name missing or 'unknown_service'; cost cannot be "
                "attributed to a team or system",
                attribute="service.name",
            )
        )

    # -- passthrough cost --------------------------------------------------
    # OpenInference is the only dialect carrying USD, and Langfuse hides it in a
    # JSON blob. Take either when present; pricing.py fills the gap otherwise.
    for dialect in search_order:
        for source_key, target in dialect.cost_keys.items():
            if source_key in attrs and target not in result.attributes:
                amount = _coerce_float(attrs[source_key])
                if amount is not None:
                    result.attributes[target] = amount
                    result.attributes[Rosetta.COST_SOURCE.value] = f"passthrough:{dialect.name}"
        for target, (source_key, json_key) in dialect.json_cost.items():
            if target in result.attributes:
                continue
            blob = _load_json_obj(attrs.get(source_key))
            if blob is None:
                continue
            amount = _coerce_float(blob.get(json_key))
            if amount is not None:
                result.attributes[target] = amount
                result.attributes[Rosetta.COST_SOURCE.value] = f"passthrough:{dialect.name}"

    # -- stamp -------------------------------------------------------------
    result.attributes[Rosetta.NORMALIZED.value] = True
    if result.dialects:
        result.attributes[Rosetta.DIALECT.value] = ",".join(result.dialects)
    result.attributes[Rosetta.CONFORMANCE_SCORE.value] = result.score
    codes = result.issue_codes()
    if codes:
        result.attributes[Rosetta.CONFORMANCE_ISSUES.value] = ",".join(codes)

    return result


def apply(
    attrs: MutableMapping[str, Any],
    *,
    resource_attrs: Mapping[str, Any] | None = None,
) -> Normalized:
    """Normalise and write the canonical attributes back into ``attrs`` in place."""
    result = normalize(attrs, resource_attrs=resource_attrs)
    attrs.update(result.attributes)
    return result
