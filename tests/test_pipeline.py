"""End-to-end tests for the Rosetta span pipeline.

The most important test here is :func:`test_enrichment_reaches_the_exporter`.
An earlier version of the processor mutated span attributes key-by-key, which
silently did nothing: a finished span's attributes are a ``BoundedAttributes``
built with ``immutable=True``, so every assignment raised and was swallowed. The
unit tests on the normaliser all passed, the demo reported correct totals from
its in-process counters, and *nothing* arrived in the backend. Only an assertion
against what the exporter actually received catches that.
"""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rosetta import Canon, Code, Rosetta, normalize
from rosetta.pricing import PricingTable
from rosetta.processor import build_provider
from rosetta.redact import redact_text, scan


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def exported():
    """A provider wired to an in-memory exporter, returning captured spans."""
    exporter = InMemorySpanExporter()
    provider, rosetta = build_provider(
        service_name="test-agent", exporter=exporter, extra_resource={}
    )
    # Replace batching with synchronous export so assertions are deterministic.
    provider._active_span_processor._span_processors = (
        provider._active_span_processor._span_processors[0],
        SimpleSpanProcessor(exporter),
    )
    tracer = provider.get_tracer("test")
    yield tracer, exporter, rosetta
    provider.shutdown()


# ---------------------------------------------------------------------------
# The regression that matters
# ---------------------------------------------------------------------------


def test_enrichment_reaches_the_exporter(exported):
    """Canonical attributes must be present on the span the exporter receives.

    Asserting on the processor's internal counters would pass even when nothing
    is written to the span, which is exactly how the original bug survived.
    """
    tracer, exporter, _ = exported

    with tracer.start_as_current_span("chat claude-sonnet-4") as span:
        # OpenInference dialect: no gen_ai.* attributes at all.
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", "claude-sonnet-4-20250514")
        span.set_attribute("llm.provider", "anthropic")
        span.set_attribute("llm.token_count.prompt", 1000)
        span.set_attribute("llm.token_count.completion", 200)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes

    # Canonical vocabulary present...
    assert attrs[Canon.INPUT_TOKENS.value] == 1000
    assert attrs[Canon.OUTPUT_TOKENS.value] == 200
    assert attrs[Canon.REQUEST_MODEL.value] == "claude-sonnet-4-20250514"
    assert attrs[Canon.PROVIDER.value] == "anthropic"
    assert attrs[Rosetta.NORMALIZED.value] is True

    # ...cost computed, since no convention defines one...
    assert attrs[Rosetta.COST_USD.value] == pytest.approx(0.006, rel=1e-3)
    assert attrs[Rosetta.COST_SOURCE.value] == "computed"

    # ...and the source attributes are untouched, so existing dashboards live.
    assert attrs["llm.token_count.prompt"] == 1000
    assert attrs["openinference.span.kind"] == "LLM"


def test_langfuse_json_tokens_become_numbers(exported):
    """Langfuse packs token counts into a JSON string; SUM() cannot see them."""
    tracer, exporter, _ = exported

    with tracer.start_as_current_span("generation") as span:
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("langfuse.observation.model.name", "gpt-4o-mini")
        span.set_attribute(
            "langfuse.observation.usage_details",
            json.dumps({"input": 1200, "output": 350}),
        )

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs[Canon.INPUT_TOKENS.value] == 1200
    assert isinstance(attrs[Canon.INPUT_TOKENS.value], int)
    assert attrs[Rosetta.COST_USD.value] == pytest.approx(0.00039, rel=1e-3)


def test_secrets_are_redacted_before_export(exported):
    """A leaked credential must never reach the exporter."""
    tracer, exporter, _ = exported
    secret = "AKIA3MJQ7XZK2LPWVN4D"

    with tracer.start_as_current_span("chat gpt-4o") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "gpt-4o")
        span.set_attribute("gen_ai.usage.input_tokens", 100)
        span.set_attribute("gen_ai.usage.output_tokens", 10)
        span.set_attribute("gen_ai.input.messages", f"here is the key {secret} ok")

    attrs = exporter.get_finished_spans()[0].attributes
    assert secret not in attrs["gen_ai.input.messages"]
    assert "[REDACTED:aws_access_key_id]" in attrs["gen_ai.input.messages"]
    assert attrs[Rosetta.REDACTED.value] is True
    assert "aws_access_key_id" in attrs[Rosetta.REDACTED_KINDS.value]


# ---------------------------------------------------------------------------
# Dark cost
# ---------------------------------------------------------------------------


def test_dark_cost_flagged_for_inference_without_usage():
    result = normalize(
        {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o"},
        resource_attrs={"service.name": "svc"},
    )
    assert result.attributes.get(Rosetta.DARK_COST.value) is True


@pytest.mark.parametrize(
    "operation", ["invoke_agent", "execute_tool", "invoke_workflow", "plan"]
)
def test_container_spans_are_not_dark_cost(operation):
    """Agent and tool spans carry no tokens by design.

    Flagging them produced one false positive per agent run, which drowned the
    real finding entirely.
    """
    result = normalize(
        {"gen_ai.operation.name": operation, "gen_ai.agent.name": "Concierge"},
        resource_attrs={"service.name": "svc"},
    )
    assert Rosetta.DARK_COST.value not in result.attributes


# ---------------------------------------------------------------------------
# Cost correctness
# ---------------------------------------------------------------------------


def test_unknown_model_is_never_priced_at_zero():
    """Guessing $0 for an unknown model would hide real spend."""
    table = PricingTable.load()
    attrs = {
        "gen_ai.request.model": "acme-secret-v9",
        "gen_ai.usage.input_tokens": 50_000,
        "gen_ai.usage.output_tokens": 9_000,
    }
    breakdown = table.apply(attrs)
    assert breakdown.source is None
    assert Rosetta.COST_USD.value not in attrs
    assert attrs[Rosetta.UNPRICED_MODEL.value] is True


def test_dark_cost_span_is_not_also_reported_as_unpriced():
    """A priceable model with no tokens is dark cost, not an unpriced model.

    Conflating the two made every dark-cost span double-report, so the
    "which models can't I price" panel listed gpt-4o -- which prices fine.
    """
    table = PricingTable.load()
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o"}
    result = normalize(attrs, resource_attrs={"service.name": "svc"})
    attrs.update(result.attributes)
    breakdown = table.apply(attrs)

    assert attrs[Rosetta.DARK_COST.value] is True
    assert breakdown.unpriced_model is False
    assert Rosetta.UNPRICED_MODEL.value not in attrs


def test_dated_model_suffix_resolves():
    table = PricingTable.load()
    assert table.resolve("gpt-4o-2024-11-20").name == "gpt-4o"
    assert table.resolve("claude-sonnet-4-20250514").name == "claude-sonnet-4"
    assert table.resolve("models/gemini-2.5-pro").name == "gemini-2.5-pro"
    # Longest prefix wins: mini must not resolve to the base model.
    assert table.resolve("gpt-4o-mini-2024-07-18").name == "gpt-4o-mini"


def test_cached_tokens_are_not_billed_twice():
    """Cache reads bill at the cache rate and are removed from base input."""
    table = PricingTable.load()
    attrs = {
        "gen_ai.request.model": "claude-sonnet-4",
        "gen_ai.usage.input_tokens": 100_000,
        "gen_ai.usage.output_tokens": 1_000,
        "gen_ai.usage.cache_read_input_tokens": 90_000,
        "gen_ai.usage.cache_write_input_tokens": 5_000,
    }
    breakdown = table.apply(attrs)
    # 5k base @3.00 + 90k read @0.30 + 5k write @3.75 + 1k out @15.00
    expected = (5_000 * 3.00 + 90_000 * 0.30 + 5_000 * 3.75 + 1_000 * 15.00) / 1e6
    assert breakdown.total_usd == pytest.approx(expected, rel=1e-9)


def test_duplicate_token_names_are_reported():
    """AWS Strands emits prompt_tokens AND input_tokens on the same span.

    Resolution picks one -- correctly -- but a hand-written query that adds both
    double-counts, so the duplicate has to be surfaced rather than hidden.
    """
    result = normalize(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1_000,
            "gen_ai.usage.prompt_tokens": 1_000,
        },
        resource_attrs={"service.name": "svc"},
    )
    assert result.attributes[Canon.INPUT_TOKENS.value] == 1_000
    assert Code.CONFLICTING_USAGE in result.issue_codes()
    conflict = next(f for f in result.findings if f.code == Code.CONFLICTING_USAGE)
    assert conflict.severity == "warning"


def test_disagreeing_token_counts_are_an_error():
    """Two names, two different numbers: the emitter is broken, not just noisy."""
    result = normalize(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1_000,
            "gen_ai.usage.prompt_tokens": 1_400,
        },
        resource_attrs={"service.name": "svc"},
    )
    conflict = next(f for f in result.findings if f.code == Code.CONFLICTING_USAGE)
    assert conflict.severity == "error"


def test_aggregate_usage_is_flagged_not_summed():
    """Pydantic AI roll-ups would double-count every token in the trace."""
    result = normalize(
        {
            "gen_ai.provider.name": "openai",
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.aggregated_usage.input_tokens": 5_000,
            "gen_ai.aggregated_usage.output_tokens": 900,
        },
        resource_attrs={"service.name": "svc"},
    )
    assert result.is_aggregate is True
    assert result.attributes[Canon.INPUT_TOKENS.value] == 5_000


# ---------------------------------------------------------------------------
# Redaction precision
# ---------------------------------------------------------------------------


def test_luhn_guards_credit_card_false_positives():
    """A 16-digit order number must survive; a real card number must not."""
    assert "credit_card" not in scan("order 1234567812345678 shipped")
    assert "credit_card" in scan("card 4111111111111111 declined")


def test_redaction_keeps_the_kind():
    cleaned, counts = redact_text("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij")
    assert counts["jwt"] == 1
    assert "[REDACTED:jwt]" in cleaned


def test_non_genai_spans_are_untouched(exported):
    tracer, exporter, rosetta = exported
    with tracer.start_as_current_span("GET /healthz") as span:
        span.set_attribute("http.request.method", "GET")

    attrs = exporter.get_finished_spans()[0].attributes
    assert Rosetta.NORMALIZED.value not in attrs
    assert rosetta.stats["genai_spans"] == 0
