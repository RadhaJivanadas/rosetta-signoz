"""Canonical GenAI vocabulary and the dialects that fail to speak it.

Why this module exists
----------------------
As of July 2026 the OpenTelemetry GenAI semantic conventions are *not stable*.
Every attribute in the registry is marked ``development``, the conventions were
moved out of ``open-telemetry/semantic-conventions`` at core v1.42.0 into a
dedicated ``semantic-conventions-genai`` repository that has **no tagged release
and a literal "TODO" schema URL**, and the names have churned repeatedly:

* ``gen_ai.system``                 -> ``gen_ai.provider.name``   (core v1.37.0)
* ``gen_ai.usage.prompt_tokens``    -> ``gen_ai.usage.input_tokens``  (v1.27.0)
* ``gen_ai.usage.completion_tokens``-> ``gen_ai.usage.output_tokens``
* per-message log events            -> ``gen_ai.input.messages`` / ``output.messages``

Meanwhile the emitter ecosystem never converged. Three mutually incompatible
vocabularies are in wide production use, and a single company routinely runs all
three at once because different frameworks ship different instrumentation:

* **OpenInference** (Arize/Phoenix) emits an entirely separate namespace and has
  *not* adopted ``gen_ai.*`` at all -- ``llm.token_count.prompt``,
  ``openinference.span.kind``. It is the only one carrying USD cost natively.
* **OpenLLMetry** (Traceloop) emits *legacy* ``gen_ai.*`` names plus proprietary
  ``llm.*`` and ``traceloop.*``.
* **Pydantic AI / Logfire** emits current ``gen_ai.*`` at core v1.37.0, plus
  ``gen_ai.aggregated_usage.*`` roll-ups on agent-run spans.

An OTLP-native backend such as SigNoz stores what it receives. It does not
rewrite vendor attributes on ingest, so "what did all my agents cost last hour"
is not expressible as one query across a polyglot fleet. Rosetta closes that gap
in the pipeline instead of in the backend, which keeps the result vendor-neutral.

Design rules
------------
1. Normalisation is *additive and non-destructive*. Source attributes are never
   deleted, so an existing dashboard built on a vendor namespace keeps working.
   Rosetta only ever adds canonical keys (and redaction markers).
2. Resolution is **first-match-wins over an explicit precedence list**, mirroring
   the approach Langfuse documents for multi-convention ingest: current standard
   names beat legacy standard names beat vendor names.
3. Token counts are **never summed across sources**. See ``AGGREGATE_HAZARD``.
4. Every decision is recorded as provenance so the conformance report can explain
   *why* a value was chosen, not just what it became.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Canonical vocabulary.
#
# Target is the current (post core-v1.37.0) OTel GenAI spelling. These are the
# only keys downstream dashboards, alerts and queries are allowed to reference,
# which is what makes a single query work across every framework.
# ---------------------------------------------------------------------------


class Canon(str, Enum):
    """Canonical attribute keys written by Rosetta."""

    PROVIDER = "gen_ai.provider.name"
    OPERATION = "gen_ai.operation.name"
    REQUEST_MODEL = "gen_ai.request.model"
    RESPONSE_MODEL = "gen_ai.response.model"

    INPUT_TOKENS = "gen_ai.usage.input_tokens"
    OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    REASONING_TOKENS = "gen_ai.usage.reasoning.output_tokens"
    CACHE_READ_TOKENS = "gen_ai.usage.cache_read_input_tokens"
    CACHE_WRITE_TOKENS = "gen_ai.usage.cache_write_input_tokens"

    AGENT_NAME = "gen_ai.agent.name"
    AGENT_VERSION = "gen_ai.agent.version"
    TOOL_NAME = "gen_ai.tool.name"
    CONVERSATION_ID = "gen_ai.conversation.id"

    INPUT_MESSAGES = "gen_ai.input.messages"
    OUTPUT_MESSAGES = "gen_ai.output.messages"
    SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"

    def __str__(self) -> str:  # keeps f-strings readable
        return self.value


# Rosetta's own namespace. The OTel GenAI spec defines **no cost attribute at
# all**, so USD cannot be canonical -- it is explicitly ours, and we say so.
class Rosetta(str, Enum):
    """Attributes Rosetta contributes that no upstream convention defines."""

    COST_USD = "rosetta.cost.usd"
    COST_INPUT_USD = "rosetta.cost.input_usd"
    COST_OUTPUT_USD = "rosetta.cost.output_usd"
    COST_SOURCE = "rosetta.cost.source"          # "computed" | "passthrough:<dialect>"
    PRICING_VERSION = "rosetta.pricing.version"

    DIALECT = "rosetta.dialect"                  # detected source dialect(s)
    NORMALIZED = "rosetta.normalized"            # bool marker
    CONFORMANCE_SCORE = "rosetta.conformance.score"
    CONFORMANCE_ISSUES = "rosetta.conformance.issues"

    DARK_COST = "rosetta.dark_cost"              # LLM span with no usage at all
    UNPRICED_MODEL = "rosetta.unpriced_model"    # tokens known, price unknown

    REDACTED = "rosetta.redacted"
    REDACTED_KINDS = "rosetta.redacted.kinds"
    REDACTED_COUNT = "rosetta.redacted.count"

    def __str__(self) -> str:
        return self.value


#: Canonical keys that represent billable token counts.
TOKEN_KEYS: tuple[Canon, ...] = (
    Canon.INPUT_TOKENS,
    Canon.OUTPUT_TOKENS,
    Canon.REASONING_TOKENS,
    Canon.CACHE_READ_TOKENS,
    Canon.CACHE_WRITE_TOKENS,
)

#: Operation names defined by the spec. Used for conformance checking.
KNOWN_OPERATIONS: frozenset[str] = frozenset(
    {
        "chat",
        "embeddings",
        "execute_tool",
        "invoke_agent",
        "create_agent",
        "invoke_workflow",
        "plan",
        "generate_content",
    }
)

AGGREGATE_HAZARD = """\
Pydantic AI emits gen_ai.aggregated_usage.* on the agent-run span as a roll-up of
its children, a deliberate deviation from spec. Reading both it and the child
gen_ai.usage.* into one canonical field would double-count every token and every
dollar. Rosetta therefore treats aggregated usage as a *fallback only* -- used
when a span has no usage of its own -- and marks the span so aggregation queries
can exclude it. This is the single most expensive mistake in polyglot GenAI cost
reporting."""


# ---------------------------------------------------------------------------
# Dialects
# ---------------------------------------------------------------------------


#: A value buried inside a JSON-encoded string attribute: (attribute, json key).
#: Langfuse packs all token counts this way, which makes them unqueryable as
#: numbers in any OTLP backend -- you cannot SUM a string.
JsonRef = tuple[str, str]


@dataclass(frozen=True)
class Dialect:
    """A source vocabulary Rosetta can read.

    ``aliases`` maps a canonical key to the source keys that may carry it, in
    precedence order. ``markers`` are attributes whose mere presence identifies
    the dialect.
    """

    name: str
    markers: tuple[str, ...]
    aliases: Mapping[Canon, tuple[str, ...]]
    #: Canonical keys whose value must be pulled out of a JSON-encoded string.
    #: Applied only after every plain alias has missed.
    json_aliases: Mapping[Canon, tuple[JsonRef, ...]] = field(default_factory=dict)
    #: Source keys holding free-text prompt/completion content, which is where
    #: secrets and PII leak. Consumed by the redaction pass.
    content_keys: tuple[str, ...] = ()
    #: Optional USD cost already present in the source (only OpenInference).
    cost_keys: Mapping[str, str] = field(default_factory=dict)
    #: Cost buried in a JSON string (Langfuse ``cost_details``).
    json_cost: Mapping[str, JsonRef] = field(default_factory=dict)
    notes: str = ""

    def detect(self, attrs: Mapping[str, Any]) -> bool:
        return any(m in attrs for m in self.markers)


# -- current standard -------------------------------------------------------

OTEL_CURRENT = Dialect(
    name="otel-genai-current",
    markers=(Canon.PROVIDER.value, Canon.INPUT_MESSAGES.value),
    aliases={
        Canon.PROVIDER: (Canon.PROVIDER.value,),
        Canon.OPERATION: (Canon.OPERATION.value,),
        Canon.REQUEST_MODEL: (Canon.REQUEST_MODEL.value,),
        Canon.RESPONSE_MODEL: (Canon.RESPONSE_MODEL.value,),
        Canon.INPUT_TOKENS: (Canon.INPUT_TOKENS.value,),
        Canon.OUTPUT_TOKENS: (Canon.OUTPUT_TOKENS.value,),
        Canon.REASONING_TOKENS: (Canon.REASONING_TOKENS.value,),
        Canon.CACHE_READ_TOKENS: (Canon.CACHE_READ_TOKENS.value,),
        Canon.CACHE_WRITE_TOKENS: (Canon.CACHE_WRITE_TOKENS.value,),
        Canon.AGENT_NAME: (Canon.AGENT_NAME.value,),
        Canon.AGENT_VERSION: (Canon.AGENT_VERSION.value,),
        Canon.TOOL_NAME: (Canon.TOOL_NAME.value,),
        Canon.CONVERSATION_ID: (Canon.CONVERSATION_ID.value,),
        Canon.INPUT_MESSAGES: (Canon.INPUT_MESSAGES.value,),
        Canon.OUTPUT_MESSAGES: (Canon.OUTPUT_MESSAGES.value,),
        Canon.SYSTEM_INSTRUCTIONS: (Canon.SYSTEM_INSTRUCTIONS.value,),
    },
    content_keys=(
        Canon.INPUT_MESSAGES.value,
        Canon.OUTPUT_MESSAGES.value,
        Canon.SYSTEM_INSTRUCTIONS.value,
    ),
    notes="Reference spelling. Still `development` in the spec -- nothing is stable.",
)

# -- legacy standard --------------------------------------------------------
# Emitted by anything pinned below core v1.37.0, and by OpenLLMetry today.

OTEL_LEGACY = Dialect(
    name="otel-genai-legacy",
    markers=("gen_ai.system", "gen_ai.usage.prompt_tokens", "gen_ai.prompt"),
    aliases={
        Canon.PROVIDER: ("gen_ai.system",),
        Canon.INPUT_TOKENS: ("gen_ai.usage.prompt_tokens",),
        Canon.OUTPUT_TOKENS: ("gen_ai.usage.completion_tokens",),
        Canon.INPUT_MESSAGES: ("gen_ai.prompt",),
        Canon.OUTPUT_MESSAGES: ("gen_ai.completion",),
    },
    content_keys=("gen_ai.prompt", "gen_ai.completion"),
    notes=(
        "gen_ai.system and gen_ai.provider.name are frequently emitted together "
        "during migration. Coalesce by precedence -- never treat as two fields."
    ),
)

# -- OpenInference (Arize / Phoenix) ---------------------------------------
# Zero gen_ai.* adoption. Verified against the semconv source: grepping for
# `gen_ai.` in openinference-semantic-conventions returns nothing.

OPENINFERENCE = Dialect(
    name="openinference",
    markers=("openinference.span.kind", "llm.token_count.prompt", "llm.model_name"),
    aliases={
        Canon.PROVIDER: ("llm.provider", "llm.system"),
        Canon.REQUEST_MODEL: ("llm.model_name",),
        Canon.INPUT_TOKENS: ("llm.token_count.prompt",),
        Canon.OUTPUT_TOKENS: ("llm.token_count.completion",),
        Canon.REASONING_TOKENS: ("llm.token_count.completion_details.reasoning",),
        Canon.CACHE_READ_TOKENS: ("llm.token_count.prompt_details.cache_read",),
        Canon.CACHE_WRITE_TOKENS: ("llm.token_count.prompt_details.cache_write",),
        Canon.TOOL_NAME: ("tool.name", "tool_call.function.name"),
        Canon.AGENT_NAME: ("agent.name",),
        Canon.CONVERSATION_ID: ("session.id",),
        Canon.INPUT_MESSAGES: ("llm.input_messages", "llm.prompts", "input.value"),
        Canon.OUTPUT_MESSAGES: ("llm.output_messages", "llm.choices", "output.value"),
    },
    content_keys=(
        "llm.input_messages",
        "llm.output_messages",
        "llm.prompts",
        "input.value",
        "output.value",
    ),
    cost_keys={
        "llm.cost.prompt": Rosetta.COST_INPUT_USD.value,
        "llm.cost.completion": Rosetta.COST_OUTPUT_USD.value,
        "llm.cost.total": Rosetta.COST_USD.value,
    },
    notes=(
        "The only dialect carrying USD natively, but the figures are usually "
        "filled in server-side by Phoenix pricing tables -- exporting straight to "
        "a neutral backend commonly yields tokens with empty cost."
    ),
)

# -- OpenLLMetry (Traceloop) ----------------------------------------------

OPENLLMETRY = Dialect(
    name="openllmetry",
    markers=("traceloop.span.kind", "traceloop.workflow.name", "llm.request.type"),
    aliases={
        Canon.PROVIDER: ("gen_ai.system",),
        Canon.REQUEST_MODEL: ("gen_ai.request.model",),
        Canon.RESPONSE_MODEL: ("gen_ai.response.model",),
        Canon.INPUT_TOKENS: ("gen_ai.usage.prompt_tokens",),
        Canon.OUTPUT_TOKENS: ("gen_ai.usage.completion_tokens",),
        Canon.CACHE_READ_TOKENS: ("gen_ai.usage.cache_read_input_tokens",),
        Canon.TOOL_NAME: ("traceloop.entity.name",),
        Canon.AGENT_NAME: ("traceloop.workflow.name",),
        Canon.INPUT_MESSAGES: ("gen_ai.prompt", "traceloop.entity.input"),
        Canon.OUTPUT_MESSAGES: ("gen_ai.completion", "traceloop.entity.output"),
    },
    content_keys=(
        "gen_ai.prompt",
        "gen_ai.completion",
        "traceloop.entity.input",
        "traceloop.entity.output",
    ),
    notes=(
        "Content capture defaults to ON here (TRACELOOP_TRACE_CONTENT), the "
        "opposite of the OTel default -- so this is the dialect most likely to "
        "carry raw prompts, and therefore leaked secrets, into storage."
    ),
)

# -- Pydantic AI / Logfire -------------------------------------------------

PYDANTIC_AI = Dialect(
    name="pydantic-ai",
    markers=("pydantic_ai.all_messages", "logfire.span_type", "gen_ai.aggregated_usage.input_tokens"),
    aliases={
        Canon.PROVIDER: (Canon.PROVIDER.value, "gen_ai.system"),
        Canon.OPERATION: (Canon.OPERATION.value,),
        Canon.REQUEST_MODEL: (Canon.REQUEST_MODEL.value,),
        Canon.RESPONSE_MODEL: (Canon.RESPONSE_MODEL.value,),
        # Own usage first; aggregated roll-up only as a fallback. See AGGREGATE_HAZARD.
        Canon.INPUT_TOKENS: (Canon.INPUT_TOKENS.value, "gen_ai.aggregated_usage.input_tokens"),
        Canon.OUTPUT_TOKENS: (Canon.OUTPUT_TOKENS.value, "gen_ai.aggregated_usage.output_tokens"),
        Canon.AGENT_NAME: (Canon.AGENT_NAME.value,),
        Canon.TOOL_NAME: (Canon.TOOL_NAME.value,),
        Canon.INPUT_MESSAGES: (Canon.INPUT_MESSAGES.value, "pydantic_ai.all_messages"),
        Canon.OUTPUT_MESSAGES: (Canon.OUTPUT_MESSAGES.value,),
        Canon.SYSTEM_INSTRUCTIONS: (Canon.SYSTEM_INSTRUCTIONS.value,),
    },
    content_keys=(
        Canon.INPUT_MESSAGES.value,
        Canon.OUTPUT_MESSAGES.value,
        "pydantic_ai.all_messages",
    ),
    notes=AGGREGATE_HAZARD,
)


# -- Langfuse SDK v4 -------------------------------------------------------
# The pathological case, and the clearest argument for normalising in the
# pipeline. Langfuse emits *no* gen_ai.* at all, and packs token counts and cost
# into JSON-encoded strings:
#
#     langfuse.observation.usage_details = '{"input": 10, "output": 5}'
#
# An OTLP backend stores that as a string. `SUM(input_tokens)` is impossible --
# the number is not a number. Unpacking it is pure, and large, value add.

LANGFUSE = Dialect(
    name="langfuse",
    markers=(
        "langfuse.observation.type",
        "langfuse.observation.model.name",
        "langfuse.observation.usage_details",
    ),
    aliases={
        Canon.REQUEST_MODEL: ("langfuse.observation.model.name",),
        Canon.INPUT_MESSAGES: ("langfuse.observation.input", "langfuse.trace.input"),
        Canon.OUTPUT_MESSAGES: ("langfuse.observation.output", "langfuse.trace.output"),
        Canon.AGENT_NAME: ("langfuse.trace.name",),
    },
    json_aliases={
        Canon.INPUT_TOKENS: (("langfuse.observation.usage_details", "input"),),
        Canon.OUTPUT_TOKENS: (("langfuse.observation.usage_details", "output"),),
        Canon.CACHE_READ_TOKENS: (("langfuse.observation.usage_details", "cache_read"),),
        Canon.REASONING_TOKENS: (("langfuse.observation.usage_details", "reasoning"),),
    },
    json_cost={
        Rosetta.COST_INPUT_USD.value: ("langfuse.observation.cost_details", "input"),
        Rosetta.COST_OUTPUT_USD.value: ("langfuse.observation.cost_details", "output"),
    },
    content_keys=(
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
    ),
    notes=(
        "Emits zero gen_ai.* attributes. Token counts and cost are JSON strings, "
        "so they are unqueryable as numbers until unpacked. cost_details is only "
        "populated if the caller passed it -- exporting to a neutral backend "
        "normally yields no cost at all, since Langfuse prices server-side."
    ),
)

# -- LangSmith OTel exporter ----------------------------------------------
# A hybrid: real numeric gen_ai.usage.* (good -- directly queryable) but the
# *older* generation of names alongside its own langsmith.* namespace.

LANGSMITH = Dialect(
    name="langsmith",
    markers=("langsmith.span.kind", "langsmith.trace.name", "langsmith.internal_provider"),
    aliases={
        Canon.PROVIDER: ("gen_ai.system",),
        Canon.OPERATION: (Canon.OPERATION.value,),
        Canon.REQUEST_MODEL: (Canon.REQUEST_MODEL.value,),
        Canon.RESPONSE_MODEL: (Canon.RESPONSE_MODEL.value,),
        # Numeric and already canonically spelled -- the one thing LangSmith
        # gets right that Langfuse does not.
        Canon.INPUT_TOKENS: (Canon.INPUT_TOKENS.value,),
        Canon.OUTPUT_TOKENS: (Canon.OUTPUT_TOKENS.value,),
        Canon.AGENT_NAME: ("langsmith.trace.name",),
        Canon.CONVERSATION_ID: ("langsmith.trace.session_id",),
        Canon.INPUT_MESSAGES: ("gen_ai.prompt",),
        Canon.OUTPUT_MESSAGES: ("gen_ai.completion",),
    },
    content_keys=("gen_ai.prompt", "gen_ai.completion"),
    notes=(
        "Uses legacy gen_ai.system and gen_ai.prompt/completion rather than "
        "provider.name and input/output.messages. Cost is computed server-side "
        "by LangSmith and is absent from the exported spans."
    ),
)


#: Multi-agent handoff attributes. Not in the spec; introduced by Traceloop's
#: OpenAI Agents instrumentation. Retained because a broken handoff is one of the
#: few failure modes only visible in the *relationship* between spans.
HANDOFF_KEYS: tuple[str, ...] = (
    "gen_ai.handoff.from_agent",
    "gen_ai.handoff.to_agent",
    "gen_ai.agent.handoff_parent",
)


#: Attributes that mark a token count as an aggregate roll-up rather than a leaf
#: measurement. Spans resolved from these are flagged so cost aggregation can
#: exclude them instead of double-counting.
AGGREGATE_SOURCE_KEYS: frozenset[str] = frozenset(
    {"gen_ai.aggregated_usage.input_tokens", "gen_ai.aggregated_usage.output_tokens"}
)


#: Resolution precedence. Earlier dialects win a contested canonical key.
#: Current standard > legacy standard > vendor namespaces.
DIALECTS: tuple[Dialect, ...] = (
    OTEL_CURRENT,
    PYDANTIC_AI,
    LANGSMITH,
    OTEL_LEGACY,
    OPENLLMETRY,
    OPENINFERENCE,
    LANGFUSE,
)

DIALECTS_BY_NAME: Mapping[str, Dialect] = {d.name: d for d in DIALECTS}


# ---------------------------------------------------------------------------
# Span-kind unification
#
# Each dialect names the *shape* of a span differently. Collapsing these into one
# axis is what lets a single query say "all tool calls, regardless of framework".
# ---------------------------------------------------------------------------

#: Canonical span roles, keyed off gen_ai.operation.name where possible.
SPAN_KIND_MAP: Mapping[str, str] = {
    # OpenInference: openinference.span.kind
    "LLM": "chat",
    "EMBEDDING": "embeddings",
    "TOOL": "execute_tool",
    "AGENT": "invoke_agent",
    "CHAIN": "invoke_workflow",
    "RETRIEVER": "retrieve",
    "RERANKER": "retrieve",
    "GUARDRAIL": "guardrail",
    "EVALUATOR": "evaluate",
    # OpenLLMetry: traceloop.span.kind
    "workflow": "invoke_workflow",
    "task": "invoke_workflow",
    "agent": "invoke_agent",
    "tool": "execute_tool",
}

SPAN_KIND_SOURCE_KEYS: tuple[str, ...] = (
    "openinference.span.kind",
    "traceloop.span.kind",
)


def infer_operation(attrs: Mapping[str, Any]) -> str | None:
    """Best-effort canonical ``gen_ai.operation.name`` for a span.

    Prefers the spec attribute; otherwise translates a vendor span-kind.
    """
    explicit = attrs.get(Canon.OPERATION.value)
    if isinstance(explicit, str) and explicit:
        return explicit
    for key in SPAN_KIND_SOURCE_KEYS:
        raw = attrs.get(key)
        if isinstance(raw, str) and raw in SPAN_KIND_MAP:
            return SPAN_KIND_MAP[raw]
    return None


def detect_dialects(attrs: Mapping[str, Any]) -> tuple[Dialect, ...]:
    """Every dialect whose markers appear on the span, in precedence order.

    More than one is normal and is itself a finding: a span carrying both
    ``gen_ai.system`` and ``gen_ai.provider.name`` is mid-migration.
    """
    return tuple(d for d in DIALECTS if d.detect(attrs))


def is_genai_span(attrs: Mapping[str, Any]) -> bool:
    """Whether the span looks like a GenAI operation in *any* known dialect.

    Deliberately broad: this is the denominator for dark-cost detection, so a
    false negative here hides real spend.
    """
    if any(d.detect(attrs) for d in DIALECTS):
        return True
    return any(
        k in attrs
        for k in (
            Canon.REQUEST_MODEL.value,
            Canon.OPERATION.value,
            "llm.model_name",
            "gen_ai.request.model",
        )
    )


def all_content_keys() -> frozenset[str]:
    """Union of every free-text content key across dialects (redaction targets)."""
    keys: set[str] = set()
    for d in DIALECTS:
        keys.update(d.content_keys)
    return frozenset(keys)
