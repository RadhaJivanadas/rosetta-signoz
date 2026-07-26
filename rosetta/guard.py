"""Runtime enforcement built on the canonical vocabulary.

Observation is not ownership. The hackathon's framing is "if you can't observe
your AI agents, you don't own them" -- but an agent you can only *watch* burn
money is still not one you own. This module closes the loop: detect, then act.

**Why this belongs in Rosetta rather than in a framework.** A budget breaker
needs to know what a run has cost. In a polyglot fleet that number does not
exist until something normalises it: `checkout-agent` reports
`gen_ai.usage.input_tokens`, `support-agent` reports `llm.token_count.prompt`,
`billing-agent` reports a JSON string, and none of them report dollars at all.
A breaker written against any single framework protects one service out of
four. Written against the canonical vocabulary, the same rule covers all of
them -- which is the whole argument for normalising in the first place.

Three conditions trip the breaker:

* **Budget** -- accumulated USD for one agent run crosses a ceiling.
* **Tool loop** -- the same tool called with the same arguments N times. Each
  call looks fine on its own; only the repetition is the failure.
* **Context runaway** -- input tokens growing monotonically across calls, the
  signature of a run that appends its own failures to the prompt and re-sends.

The breaker's own decisions are emitted as telemetry, so the control loop is
observable in the same place as the thing it controls. A guardrail you cannot
audit is just an outage with extra steps.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .semconv import Canon, Rosetta

logger = logging.getLogger("rosetta.guard")


class CircuitOpen(RuntimeError):
    """Raised into an agent run when the breaker has tripped.

    Carries the verdict so the caller can log or surface the specific reason
    rather than a bare failure.
    """

    def __init__(self, verdict: "Verdict") -> None:
        super().__init__(verdict.detail)
        self.verdict = verdict


class GuardCode:
    """Trip reasons. Continue the R-series used by normalize and pricing."""

    BUDGET = "R020"
    TOOL_LOOP = "R021"
    CONTEXT_RUNAWAY = "R022"


@dataclass(frozen=True)
class Verdict:
    """The breaker's decision about one run."""

    tripped: bool
    code: str = ""
    reason: str = ""
    detail: str = ""
    cost_usd: float = 0.0
    spans: int = 0

    def __bool__(self) -> bool:
        return self.tripped


@dataclass
class RunState:
    """Accumulated state for one agent run, keyed by trace id."""

    trace_id: str
    service: str = "unknown"
    cost_usd: float = 0.0
    spans: int = 0
    tokens_in: int = 0
    #: (tool name, argument fingerprint) -> number of times seen.
    tool_calls: dict[tuple[str, str], int] = field(default_factory=dict)
    #: Input-token counts in call order, for runaway detection.
    input_series: list[int] = field(default_factory=list)
    tripped: Verdict | None = None

    def repeat_peak(self) -> tuple[str, int]:
        """The most-repeated (tool, args) pair and its count."""
        if not self.tool_calls:
            return ("", 0)
        (tool, _args), count = max(self.tool_calls.items(), key=lambda kv: kv[1])
        return (tool, count)


def _fingerprint(value: Any) -> str:
    """Stable short hash of a tool's arguments.

    Hashed rather than stored: arguments routinely contain user data, and the
    breaker only needs to know whether two calls are identical, not what they
    said.
    """
    if value is None:
        return "-"
    text = value if isinstance(value, str) else repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


class CircuitBreaker:
    """Per-run budget, loop and runaway enforcement.

    Fed by :class:`rosetta.processor.RosettaSpanProcessor` as spans end, and
    queried by agent code via :meth:`check` between steps. It cannot abort a
    call already in flight -- nothing outside the agent's own loop can -- so the
    contract is explicit: the agent asks, the breaker answers.

    Thread-safe: an agent framework may run tool calls concurrently.
    """

    def __init__(
        self,
        *,
        budget_usd: float | None = 0.50,
        max_tool_repeats: int | None = 8,
        max_context_growth: int | None = 6,
        on_trip: Callable[[Verdict, RunState], None] | None = None,
    ) -> None:
        self.budget_usd = budget_usd
        self.max_tool_repeats = max_tool_repeats
        self.max_context_growth = max_context_growth
        self._on_trip = on_trip
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()
        #: Verdicts issued, for the demo summary.
        self.trips: list[Verdict] = []

    # -- ingestion ---------------------------------------------------------

    def observe(
        self,
        trace_id: str,
        attrs: Mapping[str, Any],
        *,
        cost_usd: float | None = None,
        is_aggregate: bool = False,
    ) -> Verdict:
        """Record one finished span against its run and re-evaluate.

        ``is_aggregate`` spans are counted for structure but never for cost:
        their children already reported the same tokens, and adding both would
        trip the budget at half the real spend.
        """
        if not trace_id:
            return Verdict(False)

        with self._lock:
            run = self._runs.get(trace_id)
            if run is None:
                run = RunState(trace_id=trace_id)
                self._runs[trace_id] = run

            run.spans += 1
            service = attrs.get("service.name")
            if isinstance(service, str) and service:
                run.service = service

            if cost_usd and not is_aggregate:
                run.cost_usd += cost_usd

            tokens_in = attrs.get(Canon.INPUT_TOKENS.value)
            if isinstance(tokens_in, int) and tokens_in > 0 and not is_aggregate:
                run.tokens_in += tokens_in
                run.input_series.append(tokens_in)

            tool = attrs.get(Canon.TOOL_NAME.value)
            if isinstance(tool, str) and tool:
                key = (tool, _fingerprint(attrs.get("gen_ai.tool.call.arguments")))
                run.tool_calls[key] = run.tool_calls.get(key, 0) + 1

            verdict = self._evaluate(run)
            if verdict.tripped and run.tripped is None:
                run.tripped = verdict
                self.trips.append(verdict)
                logger.warning("rosetta.guard tripped: %s", verdict.detail)
                if self._on_trip is not None:
                    try:
                        self._on_trip(verdict, run)
                    except Exception:
                        logger.exception("rosetta.guard: on_trip callback failed")
            return run.tripped or verdict

    # -- evaluation --------------------------------------------------------

    def _evaluate(self, run: RunState) -> Verdict:
        if run.tripped is not None:
            return run.tripped

        if self.budget_usd is not None and run.cost_usd > self.budget_usd:
            return Verdict(
                True,
                GuardCode.BUDGET,
                "budget",
                (
                    f"{run.service} run exceeded the ${self.budget_usd:.2f} budget "
                    f"at ${run.cost_usd:.4f} over {run.spans} spans"
                ),
                run.cost_usd,
                run.spans,
            )

        if self.max_tool_repeats is not None:
            tool, count = run.repeat_peak()
            if count >= self.max_tool_repeats:
                return Verdict(
                    True,
                    GuardCode.TOOL_LOOP,
                    "tool_loop",
                    (
                        f"{run.service} called tool {tool!r} {count} times with "
                        "identical arguments -- the run is looping"
                    ),
                    run.cost_usd,
                    run.spans,
                )

        if self.max_context_growth is not None:
            growth = self._monotonic_growth(run.input_series)
            if growth >= self.max_context_growth:
                return Verdict(
                    True,
                    GuardCode.CONTEXT_RUNAWAY,
                    "context_runaway",
                    (
                        f"{run.service} grew its prompt on {growth} consecutive "
                        "calls -- it is re-sending its own failures, and cost "
                        "compounds every turn"
                    ),
                    run.cost_usd,
                    run.spans,
                )

        return Verdict(False, cost_usd=run.cost_usd, spans=run.spans)

    @staticmethod
    def _monotonic_growth(series: list[int]) -> int:
        """Length of the longest run of strictly increasing input-token counts."""
        best = current = 0
        for previous, nxt in zip(series, series[1:]):
            if nxt > previous:
                current += 1
                best = max(best, current)
            else:
                current = 0
        return best

    # -- agent-facing API --------------------------------------------------

    def verdict(self, trace_id: str) -> Verdict:
        with self._lock:
            run = self._runs.get(trace_id)
            if run is None:
                return Verdict(False)
            return run.tripped or Verdict(False, cost_usd=run.cost_usd, spans=run.spans)

    def check(self, trace_id: str) -> None:
        """Raise :class:`CircuitOpen` if this run has tripped.

        Called by agent code between steps. Nothing outside the agent's own
        loop can abort an in-flight model call, so enforcement is cooperative
        by construction -- which is worth stating plainly rather than implying
        a guarantee that does not exist.
        """
        current = self.verdict(trace_id)
        if current.tripped:
            raise CircuitOpen(current)

    def state(self, trace_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(trace_id)

    def reset(self, trace_id: str | None = None) -> None:
        with self._lock:
            if trace_id is None:
                self._runs.clear()
            else:
                self._runs.pop(trace_id, None)

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        with self._lock:
            runs = len(self._runs)
            spend = sum(r.cost_usd for r in self._runs.values())
        return (
            f"runs={runs} tripped={len(self.trips)} observed_spend=${spend:.6f}"
        )


def format_trip(verdict: Verdict) -> str:
    """One-line rendering used by the demo output."""
    return f"[BREAKER {verdict.code}] {verdict.detail}"
