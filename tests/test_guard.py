"""Tests for the circuit breaker.

The load-bearing test here is
:func:`test_budget_spans_dialects_because_cost_is_canonical`. It is the whole
argument for putting enforcement behind normalisation: a run whose spans arrive
in three different vocabularies still accumulates one budget. Written against
any single framework's attribute names, the same breaker would see roughly a
third of the spend and never trip.
"""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rosetta import normalize
from rosetta.guard import CircuitBreaker, CircuitOpen, GuardCode
from rosetta.pricing import PricingTable
from rosetta.processor import build_provider

TRACE = "0af7651916cd43dd8448eb211c80319c"


def _priced(attrs: dict) -> tuple[dict, float]:
    """Normalise and price a raw span, the way the processor does."""
    table = PricingTable.load()
    result = normalize(attrs, resource_attrs={"service.name": "checkout-agent"})
    attrs = {**attrs, **result.attributes, "service.name": "checkout-agent"}
    breakdown = table.apply(attrs)
    return attrs, (breakdown.total_usd if breakdown.source == "computed" else 0.0)


# ---------------------------------------------------------------------------
# The argument
# ---------------------------------------------------------------------------


def test_budget_spans_dialects_because_cost_is_canonical():
    """One budget rule covers three incompatible vocabularies."""
    guard = CircuitBreaker(budget_usd=0.10, max_tool_repeats=None, max_context_growth=None)

    spans = [
        # Pydantic AI
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 10_000,
            "gen_ai.usage.output_tokens": 2_000,
        },
        # OpenInference -- no gen_ai.* at all
        {
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o",
            "llm.token_count.prompt": 8_000,
            "llm.token_count.completion": 1_500,
        },
        # Langfuse -- tokens as a JSON string
        {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": "gpt-4o",
            "langfuse.observation.usage_details": json.dumps(
                {"input": 12_000, "output": 3_000}
            ),
        },
    ]

    verdict = None
    for raw in spans:
        attrs, cost = _priced(raw)
        assert cost > 0, "every dialect must price, or the breaker is blind to it"
        verdict = guard.observe(TRACE, attrs, cost_usd=cost)

    assert verdict.tripped
    assert verdict.code == GuardCode.BUDGET
    state = guard.state(TRACE)
    # All three contributed; a single-framework breaker would have seen one.
    assert state.spans == 3
    # gpt-4o at $2.50/$10.00 per 1M:
    #   Pydantic AI  10000 in + 2000 out = 0.025 + 0.020 = 0.045
    #   OpenInference 8000 in + 1500 out = 0.020 + 0.015 = 0.035
    #   Langfuse     12000 in + 3000 out = 0.030 + 0.030 = 0.060
    assert state.cost_usd == pytest.approx(0.140, rel=1e-6)


# ---------------------------------------------------------------------------
# Trip conditions
# ---------------------------------------------------------------------------


def test_tool_loop_trips_on_identical_arguments():
    guard = CircuitBreaker(budget_usd=None, max_tool_repeats=5, max_context_growth=None)
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "lookup_order",
        "gen_ai.tool.call.arguments": '{"order_id": "ORD-77219"}',
        "service.name": "checkout-agent",
    }
    for _ in range(4):
        assert not guard.observe(TRACE, attrs).tripped
    assert guard.observe(TRACE, attrs).code == GuardCode.TOOL_LOOP


def test_tool_loop_ignores_differing_arguments():
    """Calling one tool repeatedly with new arguments is normal agent work."""
    guard = CircuitBreaker(budget_usd=None, max_tool_repeats=3, max_context_growth=None)
    for index in range(10):
        verdict = guard.observe(
            TRACE,
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "lookup_order",
                "gen_ai.tool.call.arguments": json.dumps({"order_id": index}),
                "service.name": "checkout-agent",
            },
        )
        assert not verdict.tripped


def test_context_runaway_trips_on_monotonic_growth():
    guard = CircuitBreaker(budget_usd=None, max_tool_repeats=None, max_context_growth=4)
    verdict = None
    for tokens in (1_000, 1_500, 2_000, 2_600, 3_300):
        verdict = guard.observe(
            TRACE,
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": tokens,
                "service.name": "checkout-agent",
            },
        )
    assert verdict.code == GuardCode.CONTEXT_RUNAWAY


def test_stable_context_does_not_trip():
    guard = CircuitBreaker(budget_usd=None, max_tool_repeats=None, max_context_growth=3)
    for tokens in (1_000, 900, 1_100, 950, 1_050, 980):
        assert not guard.observe(
            TRACE,
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": tokens,
                "service.name": "checkout-agent",
            },
        ).tripped


# ---------------------------------------------------------------------------
# Correctness of accounting
# ---------------------------------------------------------------------------


def test_aggregate_spans_do_not_count_toward_budget():
    """Roll-ups would trip the budget at half the real spend."""
    guard = CircuitBreaker(budget_usd=0.05, max_tool_repeats=None, max_context_growth=None)
    attrs = {"gen_ai.request.model": "gpt-4o", "service.name": "checkout-agent"}
    guard.observe(TRACE, attrs, cost_usd=0.04, is_aggregate=True)
    assert not guard.verdict(TRACE).tripped
    assert guard.state(TRACE).cost_usd == 0.0


def test_runs_are_isolated_by_trace():
    guard = CircuitBreaker(budget_usd=0.05, max_tool_repeats=None, max_context_growth=None)
    attrs = {"gen_ai.request.model": "gpt-4o", "service.name": "checkout-agent"}
    guard.observe("a" * 32, attrs, cost_usd=0.06)
    assert guard.verdict("a" * 32).tripped
    assert not guard.verdict("b" * 32).tripped


def test_arguments_are_hashed_not_stored():
    """Tool arguments carry user data; the breaker only needs identity."""
    guard = CircuitBreaker(budget_usd=None, max_tool_repeats=2, max_context_growth=None)
    secret = '{"ssn": "123-45-6789"}'
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "lookup",
        "gen_ai.tool.call.arguments": secret,
        "service.name": "checkout-agent",
    }
    guard.observe(TRACE, attrs)
    stored = json.dumps(list(guard.state(TRACE).tool_calls.keys()))
    assert "123-45-6789" not in stored


# ---------------------------------------------------------------------------
# Agent-facing contract
# ---------------------------------------------------------------------------


def test_check_raises_once_tripped():
    guard = CircuitBreaker(budget_usd=0.01, max_tool_repeats=None, max_context_growth=None)
    guard.check(TRACE)  # not tripped: no exception
    guard.observe(TRACE, {"service.name": "x"}, cost_usd=0.5)
    with pytest.raises(CircuitOpen) as excinfo:
        guard.check(TRACE)
    assert excinfo.value.verdict.code == GuardCode.BUDGET


def test_breaker_decision_is_written_to_the_span():
    """A guardrail you cannot audit is just an outage with extra steps."""
    exporter = InMemorySpanExporter()
    guard = CircuitBreaker(
        budget_usd=0.0001, max_tool_repeats=None, max_context_growth=None
    )
    provider, _ = build_provider(
        service_name="checkout-agent", exporter=exporter, guard=guard
    )
    provider._active_span_processor._span_processors = (
        provider._active_span_processor._span_processors[0],
        SimpleSpanProcessor(exporter),
    )
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("chat gpt-4o") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "gpt-4o")
        span.set_attribute("gen_ai.usage.input_tokens", 100_000)
        span.set_attribute("gen_ai.usage.output_tokens", 10_000)

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["rosetta.guard.tripped"] is True
    assert attrs["rosetta.guard.code"] == GuardCode.BUDGET
    provider.shutdown()
