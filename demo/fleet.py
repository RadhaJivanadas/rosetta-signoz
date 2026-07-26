"""A polyglot agent fleet -- the situation Rosetta exists to fix.

Four services doing ordinary agent work, each instrumented with a different
framework and therefore emitting a different, mutually incompatible vocabulary:

===================  ==========================  =================================
service              framework / dialect         how it reports tokens
===================  ==========================  =================================
checkout-agent       Pydantic AI (Logfire)       gen_ai.usage.input_tokens
support-agent        CrewAI (OpenInference)      llm.token_count.prompt
research-agent       LangChain (OpenLLMetry)     gen_ai.usage.prompt_tokens
billing-agent        Langfuse SDK v4             JSON string, not a number
===================  ==========================  =================================

Ask "what did my agents cost this hour" and no single query can answer it. That
is the entire problem, and it is not hypothetical -- these are the real attribute
names each library emits today.

The fleet also carries four planted pathologies, each of which is invisible in a
naive setup:

* **Dark cost** -- inference spans with no token usage at all.
* **Leaked credentials** -- an AWS key and a JWT pasted into a prompt.
* **Runaway loop** -- an agent calling one tool 24 times on the same argument.
* **Unpriced model** -- real spend against a model absent from the price table.

No API keys and no network egress are required: token counts and latencies are
generated from a seeded RNG so every run is byte-identical and any judge can
reproduce the exact numbers in the README.
"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from rosetta.guard import CircuitBreaker
from rosetta.processor import build_provider

#: Fixed so the demo is reproducible. Change it and the numbers change.
SEED = 20260726


# ---------------------------------------------------------------------------
# Dialect emitters
#
# Each returns the attribute dict that its framework would really produce for a
# model call. Keeping them side by side is the clearest statement of the problem.
# ---------------------------------------------------------------------------


def attrs_pydantic_ai(model: str, n_in: int, n_out: int, **extra: Any) -> dict[str, Any]:
    """Pydantic AI / Logfire: current spec spelling."""
    return {
        "gen_ai.provider.name": extra.get("provider", "openai"),
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": n_in,
        "gen_ai.usage.output_tokens": n_out,
        "logfire.span_type": "span",
        **{k: v for k, v in extra.items() if k != "provider"},
    }


def attrs_openinference(model: str, n_in: int, n_out: int, **extra: Any) -> dict[str, Any]:
    """CrewAI via OpenInference: entirely its own namespace, zero gen_ai.*."""
    return {
        "openinference.span.kind": "LLM",
        "llm.model_name": model,
        "llm.provider": extra.get("provider", "anthropic"),
        "llm.token_count.prompt": n_in,
        "llm.token_count.completion": n_out,
        "llm.token_count.total": n_in + n_out,
        **{k: v for k, v in extra.items() if k != "provider"},
    }


def attrs_openllmetry(model: str, n_in: int, n_out: int, **extra: Any) -> dict[str, Any]:
    """LangChain via OpenLLMetry: superseded gen_ai.* names plus traceloop.*."""
    return {
        "traceloop.span.kind": "task",
        "traceloop.workflow.name": extra.get("workflow", "research-pipeline"),
        "gen_ai.system": extra.get("provider", "openai"),
        "gen_ai.request.model": model,
        "gen_ai.usage.prompt_tokens": n_in,
        "gen_ai.usage.completion_tokens": n_out,
        "llm.request.type": "chat",
        **{k: v for k, v in extra.items() if k not in ("provider", "workflow")},
    }


def attrs_langfuse(model: str, n_in: int, n_out: int, **extra: Any) -> dict[str, Any]:
    """Langfuse SDK v4: token counts encoded as a JSON *string*.

    This is the pathological case. ``langfuse.observation.usage_details`` holds
    ``{"input": 900, "output": 210}`` as text, so no backend can SUM it.
    """
    import json as _json

    usage = {"input": n_in, "output": n_out}
    return {
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": model,
        "langfuse.observation.usage_details": _json.dumps(usage),
        **extra,
    }


@dataclass(frozen=True)
class Service:
    """One instrumented service in the fleet."""

    name: str
    framework: str
    emitter: Callable[..., dict[str, Any]]
    model: str
    provider: str
    agent: str


FLEET: tuple[Service, ...] = (
    Service("checkout-agent", "Pydantic AI", attrs_pydantic_ai, "gpt-4o", "openai", "CheckoutConcierge"),
    Service("support-agent", "CrewAI / OpenInference", attrs_openinference, "claude-sonnet-4-20250514", "anthropic", "SupportTriage"),
    Service("research-agent", "LangChain / OpenLLMetry", attrs_openllmetry, "gpt-4o-mini", "openai", "ResearchPipeline"),
    Service("billing-agent", "Langfuse SDK v4", attrs_langfuse, "claude-haiku-3-5", "anthropic", "InvoiceReconciler"),
)


# ---------------------------------------------------------------------------
# Planted pathologies
# ---------------------------------------------------------------------------

#: A prompt with real-shaped credentials in it. These are syntactically valid
#: but not live keys -- AKIA + 16 chars, and an unsigned JWT.
LEAKY_PROMPT = (
    "The deploy failed again. Here is the config the user pasted:\n"
    "  AWS_ACCESS_KEY_ID=AKIA3MJQ7XZK2LPWVN4D\n"
    "  aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY123\n"
    "  session=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk\n"
    "  db=postgres://svc_billing:hunter2@10.0.4.11:5432/prod\n"
    "Customer contact is dana.whitfield@example.com, card 4111111111111111.\n"
    "Please diagnose."
)

#: A model that genuinely exists in the org but is absent from pricing.yaml.
UNPRICED_MODEL = "acme-internal-reranker-v3"


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _sleep(rng: random.Random, lo: float, hi: float) -> None:
    """Tiny real delay so spans have plausible, non-zero durations."""
    time.sleep(rng.uniform(lo, hi))


def emit_normal_run(tracer: trace.Tracer, svc: Service, rng: random.Random) -> None:
    """One healthy agent run: an agent span wrapping a tool call and a model call."""
    with tracer.start_as_current_span(
        f"invoke_agent {svc.agent}", kind=SpanKind.CLIENT
    ) as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", svc.agent)

        tool = rng.choice(["lookup_order", "search_kb", "fetch_invoice", "web_search"])
        with tracer.start_as_current_span(f"execute_tool {tool}") as tool_span:
            tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
            tool_span.set_attribute("gen_ai.tool.name", tool)
            _sleep(rng, 0.005, 0.02)

        n_in = rng.randint(600, 4200)
        n_out = rng.randint(80, 900)
        with tracer.start_as_current_span(f"chat {svc.model}") as llm_span:
            for key, value in svc.emitter(
                svc.model, n_in, n_out, provider=svc.provider
            ).items():
                llm_span.set_attribute(key, value)
            _sleep(rng, 0.01, 0.04)


def emit_dark_cost(tracer: trace.Tracer, svc: Service, rng: random.Random) -> None:
    """An inference span that reports no token usage at all.

    Real causes: a streaming response whose usage block was never read, a
    provider SDK version that stopped emitting usage, a proxy that strips it.
    The call still costs money; nothing in any dashboard will ever show it.
    """
    with tracer.start_as_current_span(f"chat {svc.model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", svc.model)
        span.set_attribute("gen_ai.provider.name", svc.provider)
        span.set_attribute("gen_ai.request.stream", True)
        _sleep(rng, 0.01, 0.03)


def emit_secret_leak(tracer: trace.Tracer, svc: Service, rng: random.Random) -> None:
    """A prompt carrying pasted credentials into the telemetry store."""
    n_in, n_out = rng.randint(1800, 2600), rng.randint(200, 500)
    with tracer.start_as_current_span(f"chat {svc.model}") as span:
        for key, value in svc.emitter(
            svc.model, n_in, n_out, provider=svc.provider
        ).items():
            span.set_attribute(key, value)
        # Written in whichever content key this dialect actually uses.
        if svc.framework.startswith("Pydantic"):
            span.set_attribute("gen_ai.input.messages", LEAKY_PROMPT)
        elif "OpenInference" in svc.framework:
            span.set_attribute("llm.input_messages", LEAKY_PROMPT)
        elif "OpenLLMetry" in svc.framework:
            span.set_attribute("gen_ai.prompt", LEAKY_PROMPT)
        else:
            span.set_attribute("langfuse.observation.input", LEAKY_PROMPT)
        _sleep(rng, 0.01, 0.03)


def emit_runaway_loop(
    tracer: trace.Tracer, svc: Service, rng: random.Random, iterations: int = 24
) -> None:
    """An agent stuck calling the same tool with the same argument.

    Each turn is individually unremarkable. Only the *shape* of the trace --
    one tool, one argument, two dozen times -- reveals the failure.
    """
    with tracer.start_as_current_span(
        f"invoke_agent {svc.agent}", kind=SpanKind.CLIENT
    ) as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", svc.agent)
        agent_span.set_attribute("rosetta.demo.scenario", "runaway_loop")

        for turn in range(iterations):
            with tracer.start_as_current_span("execute_tool lookup_order") as tool_span:
                tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
                tool_span.set_attribute("gen_ai.tool.name", "lookup_order")
                tool_span.set_attribute("gen_ai.tool.call.arguments", '{"order_id": "ORD-77219"}')
                tool_span.set_attribute("rosetta.demo.turn", turn)
                tool_span.set_status(Status(StatusCode.ERROR, "order not found"))

            n_in = 3200 + turn * 210  # context grows every turn -- cost compounds
            with tracer.start_as_current_span(f"chat {svc.model}") as llm_span:
                for key, value in svc.emitter(
                    svc.model, n_in, rng.randint(60, 160), provider=svc.provider
                ).items():
                    llm_span.set_attribute(key, value)

        agent_span.set_status(Status(StatusCode.ERROR, "max iterations exceeded"))


def emit_guarded_loop(
    tracer: trace.Tracer,
    svc: Service,
    rng: random.Random,
    guard: "CircuitBreaker",
    iterations: int = 24,
) -> tuple[int, float]:
    """The same runaway loop, but with the circuit breaker armed.

    The agent asks the breaker between turns and stops when told to. Nothing
    outside an agent's own loop can abort a model call already in flight, so
    enforcement is cooperative by construction -- the honest shape for this.

    Returns ``(turns_executed, cost_of_the_turns_avoided)``.
    """
    from rosetta.guard import CircuitOpen

    executed = 0
    with tracer.start_as_current_span(
        f"invoke_agent {svc.agent}", kind=SpanKind.CLIENT
    ) as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", svc.agent)
        agent_span.set_attribute("rosetta.demo.scenario", "guarded_loop")

        context = agent_span.get_span_context()
        trace_id = format(context.trace_id, "032x")

        for turn in range(iterations):
            try:
                guard.check(trace_id)
            except CircuitOpen as stop:
                agent_span.set_attribute("rosetta.guard.stopped_at_turn", turn)
                agent_span.set_status(Status(StatusCode.ERROR, stop.verdict.reason))
                agent_span.add_event(
                    "rosetta.circuit_open",
                    {
                        "rosetta.guard.code": stop.verdict.code,
                        "rosetta.guard.detail": stop.verdict.detail,
                    },
                )
                break

            with tracer.start_as_current_span("execute_tool lookup_order") as tool_span:
                tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
                tool_span.set_attribute("gen_ai.tool.name", "lookup_order")
                tool_span.set_attribute(
                    "gen_ai.tool.call.arguments", '{"order_id": "ORD-77219"}'
                )
                tool_span.set_status(Status(StatusCode.ERROR, "order not found"))

            n_in = 3200 + turn * 210
            with tracer.start_as_current_span(f"chat {svc.model}") as llm_span:
                for key, value in svc.emitter(
                    svc.model, n_in, rng.randint(60, 160), provider=svc.provider
                ).items():
                    llm_span.set_attribute(key, value)
            executed += 1

    # What the turns we never ran would have cost, at the same growth rate.
    from rosetta.pricing import PricingTable

    table = PricingTable.load()
    rate = table.resolve(svc.model)
    avoided = 0.0
    if rate is not None:
        for turn in range(executed, iterations):
            avoided += (3200 + turn * 210) * rate.input / 1_000_000
            avoided += 110 * rate.output / 1_000_000
    return executed, avoided


def emit_unpriced_model(tracer: trace.Tracer, svc: Service, rng: random.Random) -> None:
    """Real token spend against a model no price table knows."""
    with tracer.start_as_current_span(f"chat {UNPRICED_MODEL}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", UNPRICED_MODEL)
        span.set_attribute("gen_ai.provider.name", "self-hosted")
        span.set_attribute("gen_ai.usage.input_tokens", rng.randint(20000, 45000))
        span.set_attribute("gen_ai.usage.output_tokens", rng.randint(2000, 6000))
        _sleep(rng, 0.01, 0.03)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_service(
    svc: Service,
    *,
    endpoint: str,
    healthy_runs: int,
    scenarios: Sequence[str],
    rng: random.Random,
    guard: CircuitBreaker | None = None,
) -> tuple[dict[str, int], float, list[str]]:
    """Emit one service's traffic through its own TracerProvider."""
    notes: list[str] = []
    provider, rosetta = build_provider(
        service_name=svc.name,
        endpoint=endpoint,
        extra_resource={
            "rosetta.demo.framework": svc.framework,
            "deployment.environment": "production",
        },
        signals="all",
        guard=guard,
    )
    tracer = provider.get_tracer(f"rosetta.demo.{svc.name}")

    for _ in range(healthy_runs):
        emit_normal_run(tracer, svc, rng)

    if "dark_cost" in scenarios:
        for _ in range(3):
            emit_dark_cost(tracer, svc, rng)
    if "secret_leak" in scenarios:
        emit_secret_leak(tracer, svc, rng)
    if "runaway" in scenarios:
        emit_runaway_loop(tracer, svc, rng)
    if "guarded" in scenarios and guard is not None:
        executed, avoided = emit_guarded_loop(tracer, svc, rng, guard)
        notes.append(
            f"circuit breaker stopped the loop after {executed}/24 turns, "
            f"avoiding ~${avoided:.4f}"
        )
    if "unpriced" in scenarios:
        emit_unpriced_model(tracer, svc, rng)

    provider.force_flush()
    provider.shutdown()
    # Metrics and logs have their own providers and must be flushed separately,
    # or a short-lived process exits before the first periodic export.
    if getattr(rosetta, "meter_provider", None) is not None:
        rosetta.meter_provider.force_flush()
        rosetta.meter_provider.shutdown()
    if getattr(rosetta, "logger_provider", None) is not None:
        rosetta.logger_provider.force_flush()
        rosetta.logger_provider.shutdown()
    return dict(rosetta.stats), rosetta.total_usd, notes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit a polyglot agent fleet into SigNoz via OTLP."
    )
    parser.add_argument(
        "--endpoint",
        default="http://localhost:4318",
        help="OTLP HTTP endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--runs", type=int, default=12, help="healthy runs per service (default: %(default)s)"
    )
    parser.add_argument(
        "--scenarios",
        default="dark_cost,secret_leak,runaway,unpriced,guarded",
        help="comma-separated pathologies to plant, or 'none'",
    )
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--budget",
        type=float,
        default=0.35,
        help="per-run USD ceiling enforced by the circuit breaker",
    )
    args = parser.parse_args(argv)

    scenarios = [s for s in args.scenarios.split(",") if s and s != "none"]
    rng = random.Random(args.seed)

    print(f"Rosetta demo fleet -> {args.endpoint}")
    print(f"  seed={args.seed}  runs/service={args.runs}  scenarios={scenarios or ['none']}\n")

    grand_total = 0.0
    totals: dict[str, int] = {}
    # One breaker for the whole fleet. It can express a single budget rule
    # across four frameworks only because cost is canonical by the time it
    # sees it -- that is the point.
    guard = CircuitBreaker(budget_usd=args.budget, max_tool_repeats=8)
    all_notes: list[str] = []

    for svc in FLEET:
        # Only the first service gets the loud pathologies, so the fleet still
        # looks mostly healthy -- which is what makes them hard to spot.
        svc_scenarios = scenarios if svc.name == "checkout-agent" else [
            s for s in scenarios if s in ("dark_cost",)
        ]
        stats, usd, notes = run_service(
            svc,
            endpoint=args.endpoint,
            healthy_runs=args.runs,
            scenarios=svc_scenarios,
            rng=rng,
            guard=guard,
        )
        all_notes.extend(f"{svc.name}: {n}" for n in notes)
        grand_total += usd
        for key, value in stats.items():
            totals[key] = totals.get(key, 0) + value
        print(
            f"  {svc.name:<16} {svc.framework:<26} "
            f"genai={stats['genai_spans']:>4} priced={stats['priced']:>4} "
            f"dark={stats['dark_cost']:>3} redacted={stats['redacted']:>2} "
            f"${usd:.6f}"
        )

    print(
        f"\n  {'TOTAL':<16} {'':<26} "
        f"genai={totals.get('genai_spans', 0):>4} priced={totals.get('priced', 0):>4} "
        f"dark={totals.get('dark_cost', 0):>3} redacted={totals.get('redacted', 0):>2} "
        f"${grand_total:.6f}"
    )
    if guard.trips or all_notes:
        print(
            f"\n  circuit breaker  budget=${args.budget:.2f}/run, "
            "max 8 identical tool calls"
        )
        for note in all_notes:
            print(f"    {note}")
        seen: set[str] = set()
        for verdict in guard.trips:
            if verdict.detail in seen:
                continue
            seen.add(verdict.detail)
            print(f"    [{verdict.code}] {verdict.detail}")

    print(
        "\nEvery number above came from four services that share no attribute names.\n"
        "Open SigNoz and query gen_ai.usage.input_tokens or rosetta.cost.usd -- one\n"
        "query now spans all four frameworks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
