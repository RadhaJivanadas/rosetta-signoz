"""The OpenTelemetry span processor that ties Rosetta together.

Drop-in usage::

    from rosetta.processor import instrument
    instrument(service_name="checkout-agent")

Everything downstream -- dashboards, alerts, the MCP investigator -- depends only
on the canonical vocabulary this processor writes, which is what makes a single
query work across frameworks that share no attribute names.

Ordering matters and is fixed:

1. **Redact** first, so a secret is destroyed before anything else reads or
   copies the content.
2. **Normalise** second, producing canonical keys from whichever dialect arrived.
3. **Price** last, because pricing needs canonical model and token attributes.

The processor is deliberately synchronous and allocation-light: it runs on the
application's span-end path, so a slow processor is application latency. There
are no network calls, no model inference, and no unbounded regex backtracking.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from .guard import CircuitBreaker
from .normalize import Normalized, normalize
from .pricing import PricingTable
from .redact import redact_attributes
from .semconv import Canon, Rosetta

logger = logging.getLogger("rosetta")

#: Set ROSETTA_REDACT=0 to disable redaction (not advisable outside local demos).
_REDACT_DEFAULT = os.environ.get("ROSETTA_REDACT", "1") not in ("0", "false", "False")


class RosettaSpanProcessor(SpanProcessor):
    """Normalises, prices and redacts GenAI spans as they end.

    Attaches canonical attributes on ``on_end``. Non-GenAI spans are passed
    through untouched after a single cheap membership test.
    """

    def __init__(
        self,
        *,
        pricing: PricingTable | None = None,
        redact: bool = _REDACT_DEFAULT,
        resource_attrs: Mapping[str, Any] | None = None,
        rosetta_metrics: "RosettaMetrics | None" = None,
        governance_logger: Any = None,
        guard: "CircuitBreaker | None" = None,
    ) -> None:
        self._pricing = pricing if pricing is not None else PricingTable.load()
        self._redact = redact
        self._resource_attrs = dict(resource_attrs or {})
        self._metrics = rosetta_metrics
        self._governance_logger = governance_logger
        self._guard = guard
        #: Counters exposed for the demo's before/after summary.
        self.stats: dict[str, int] = {
            "spans_seen": 0,
            "genai_spans": 0,
            "normalized": 0,
            "priced": 0,
            "dark_cost": 0,
            "redacted": 0,
            "unpriced_model": 0,
        }
        self.total_usd: float = 0.0

    # -- SpanProcessor API -------------------------------------------------

    def on_start(self, span: Span, parent_context: Any = None) -> None:  # noqa: D102
        return None

    def on_end(self, span: ReadableSpan) -> None:  # noqa: D102
        self.stats["spans_seen"] += 1
        try:
            self._process(span)
        except Exception:  # never break the host application's export path
            logger.exception("rosetta: span processing failed; span passed through")

    def shutdown(self) -> None:  # noqa: D102
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: D102
        return True

    # -- internals ---------------------------------------------------------

    def _process(self, span: ReadableSpan) -> None:
        attrs = span.attributes
        if attrs is None:
            return

        working: dict[str, Any] = dict(attrs)

        resource_attrs = dict(self._resource_attrs)
        if span.resource is not None:
            resource_attrs.update(dict(span.resource.attributes))

        # 1. redact before anything reads the content
        if self._redact:
            redaction = redact_attributes(working)
            if redaction.total:
                self.stats["redacted"] += 1
                logger.warning(
                    "rosetta: redacted %d secret(s) of kind(s) %s from span %r",
                    redaction.total,
                    ",".join(redaction.kinds),
                    span.name,
                )

        # 2. normalise
        result: Normalized = normalize(working, resource_attrs=resource_attrs)
        if not result.is_genai:
            self._write_back(span, working)
            return

        self.stats["genai_spans"] += 1
        working.update(result.attributes)
        self.stats["normalized"] += 1
        if result.attributes.get(Rosetta.DARK_COST.value):
            self.stats["dark_cost"] += 1

        # 3. price
        breakdown = self._pricing.apply(working, is_aggregate=result.is_aggregate)
        if breakdown.source == "computed":
            self.stats["priced"] += 1
            # Roll-up spans are excluded from the running total for the same
            # reason queries must exclude them: their children already counted.
            if not result.is_aggregate:
                self.total_usd += breakdown.total_usd
        elif working.get(Rosetta.UNPRICED_MODEL.value):
            self.stats["unpriced_model"] += 1

        # Fold pricing findings into the conformance score already on the span.
        if breakdown.findings:
            existing = str(working.get(Rosetta.CONFORMANCE_ISSUES.value) or "")
            codes = [c for c in existing.split(",") if c]
            for code, _severity, _message in breakdown.findings:
                if code not in codes:
                    codes.append(code)
            working[Rosetta.CONFORMANCE_ISSUES.value] = ",".join(codes)

        # Same finding, three signal shapes: span attribute (above), metric and
        # log (below). Each is queried the way that signal is meant to be.
        codes = tuple(
            c
            for c in str(working.get(Rosetta.CONFORMANCE_ISSUES.value) or "").split(",")
            if c
        )
        if self._metrics is not None:
            try:
                self._metrics.record(
                    {**resource_attrs, **working},
                    cost_usd=(
                        breakdown.total_usd if breakdown.source == "computed" else None
                    ),
                    is_aggregate=result.is_aggregate,
                    finding_codes=codes,
                )
            except Exception:
                logger.exception("rosetta: metric recording failed")

        if self._governance_logger is not None:
            self._emit_governance_logs({**resource_attrs, **working}, codes)

        # Enforcement, fed by the *normalised* cost. This is the payoff of the
        # rest of the module: one budget rule covers every framework, because
        # by this point they all speak the same vocabulary.
        if self._guard is not None:
            try:
                context = span.get_span_context()
                trace_id = format(context.trace_id, "032x") if context else ""
                verdict = self._guard.observe(
                    trace_id,
                    {**resource_attrs, **working},
                    cost_usd=(
                        breakdown.total_usd if breakdown.source == "computed" else None
                    ),
                    is_aggregate=result.is_aggregate,
                )
                if verdict.tripped:
                    # The breaker's own decision is telemetry too; a guardrail
                    # you cannot audit is just an outage with extra steps.
                    working[Rosetta.NORMALIZED.value] = True
                    working["rosetta.guard.tripped"] = True
                    working["rosetta.guard.code"] = verdict.code
                    working["rosetta.guard.reason"] = verdict.reason
            except Exception:
                logger.exception("rosetta: circuit breaker evaluation failed")

        self._write_back(span, working)

    def _emit_governance_logs(
        self, attrs: Mapping[str, Any], codes: tuple[str, ...]
    ) -> None:
        """Emit a log record for findings that must outlive their trace.

        Traces are sampled and expire; a leaked-credential finding that vanishes
        with its span is not a security control.
        """
        from .emit import GOVERNANCE_CODES, governance_message

        for code in codes:
            if code not in GOVERNANCE_CODES:
                continue
            severity, body = governance_message(code, attrs)
            try:
                self._governance_logger.warning(
                    body,
                    extra={
                        "rosetta.finding.code": code,
                        "rosetta.finding.severity": severity,
                    },
                )
            except Exception:
                logger.exception("rosetta: governance log emission failed")

    @staticmethod
    def _write_back(span: ReadableSpan, working: Mapping[str, Any]) -> None:
        """Install the enriched attribute set on the span.

        A finished span's ``attributes`` is a ``BoundedAttributes`` created with
        ``immutable=True``, so per-key assignment raises ``TypeError`` and any
        enrichment is silently lost -- the span reaches the exporter unchanged.
        Replacing the mapping wholesale is the reliable route, and it is safe:
        ``ReadableSpan`` is a plain object and every exporter reads the
        ``attributes`` property, which returns this attribute directly.

        Ordering makes this work end-to-end: this processor is registered before
        the ``BatchSpanProcessor``, and the SDK hands the *same* ``ReadableSpan``
        instance to each processor in turn, so the exporter sees the replacement.
        """
        try:
            object.__setattr__(span, "_attributes", dict(working))
        except Exception:
            logger.exception("rosetta: could not install enriched attributes")

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        """One-line human summary, used by the demo runner."""
        s = self.stats
        return (
            f"spans={s['spans_seen']} genai={s['genai_spans']} "
            f"normalized={s['normalized']} priced={s['priced']} "
            f"dark_cost={s['dark_cost']} unpriced={s['unpriced_model']} "
            f"redacted={s['redacted']} total=${self.total_usd:.6f}"
        )


# ---------------------------------------------------------------------------
# Convenience wiring
# ---------------------------------------------------------------------------

DEFAULT_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
)


def build_provider(
    *,
    service_name: str,
    endpoint: str | None = None,
    exporter: SpanExporter | None = None,
    pricing: PricingTable | None = None,
    redact: bool = _REDACT_DEFAULT,
    extra_resource: Mapping[str, Any] | None = None,
    signals: str = "traces",
    guard: CircuitBreaker | None = None,
) -> tuple[TracerProvider, RosettaSpanProcessor]:
    """Build a TracerProvider with Rosetta in front of the OTLP exporter.

    ``signals`` selects which additional signals to emit alongside traces:
    ``"traces"`` (default), or ``"all"`` to also emit metrics and governance
    logs. Metrics and logs are skipped when a custom ``exporter`` is supplied,
    since that path is used by the tests with an in-memory span exporter.

    Returns the provider and the Rosetta processor, the latter so a caller can
    read ``stats`` / ``total_usd`` after a run.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    attributes: dict[str, Any] = {"service.name": service_name}
    attributes.update(dict(extra_resource or {}))
    resource = Resource.create(attributes)

    provider = TracerProvider(resource=resource)

    rosetta_metrics = None
    governance_logger = None
    meter_provider = None
    logger_provider = None

    if signals == "all" and exporter is None:
        from .emit import RosettaMetrics, build_logger_provider, build_meter_provider

        base = (endpoint or DEFAULT_OTLP_ENDPOINT).rstrip("/")
        meter_provider = build_meter_provider(
            service_name=service_name, endpoint=base, extra_resource=extra_resource
        )
        rosetta_metrics = RosettaMetrics(meter_provider.get_meter("rosetta"))

        logger_provider = build_logger_provider(
            service_name=service_name, endpoint=base, extra_resource=extra_resource
        )
        if logger_provider is not None:
            from opentelemetry.sdk._logs import LoggingHandler

            governance_logger = logging.getLogger(f"rosetta.governance.{service_name}")
            governance_logger.setLevel(logging.INFO)
            governance_logger.propagate = False
            governance_logger.addHandler(
                LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
            )

    rosetta = RosettaSpanProcessor(
        pricing=pricing,
        redact=redact,
        resource_attrs=attributes,
        rosetta_metrics=rosetta_metrics,
        governance_logger=governance_logger,
        guard=guard,
    )
    # Kept on the processor so callers can flush them with the provider.
    rosetta.meter_provider = meter_provider
    rosetta.logger_provider = logger_provider
    # Rosetta must run before the exporter's processor so the exporter sees the
    # enriched, redacted attributes.
    provider.add_span_processor(rosetta)

    if exporter is None:
        base = (endpoint or DEFAULT_OTLP_ENDPOINT).rstrip("/")
        traces_url = base if base.endswith("/v1/traces") else f"{base}/v1/traces"
        exporter = OTLPSpanExporter(endpoint=traces_url)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    return provider, rosetta


def instrument(
    *,
    service_name: str,
    endpoint: str | None = None,
    pricing: PricingTable | None = None,
    redact: bool = _REDACT_DEFAULT,
    extra_resource: Mapping[str, Any] | None = None,
    signals: str = "traces",
    guard: CircuitBreaker | None = None,
) -> RosettaSpanProcessor:
    """Install Rosetta globally and return the processor."""
    provider, rosetta = build_provider(
        service_name=service_name,
        endpoint=endpoint,
        pricing=pricing,
        redact=redact,
        extra_resource=extra_resource,
        signals=signals,
        guard=guard,
    )
    trace.set_tracer_provider(provider)
    return rosetta
