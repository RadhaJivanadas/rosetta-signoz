"""Metrics and logs derived from normalised spans.

Traces alone are the wrong shape for two of the questions Rosetta answers.

*Cost over time, by service and model* is an aggregation over a high-cardinality
dimension. Computing it by scanning spans works at demo scale and stops working
at production scale, so cost and tokens are also emitted as **metrics**, where
they are pre-aggregated and cheap to alert on.

*A leaked credential* is an event that must survive independently of the span
that carried it. Trace retention is typically days and is often sampled; a
security finding that vanishes with the trace is not a security control. So
governance findings are also emitted as **structured logs**, which have their
own retention and are searchable by regex.

This gives the same finding three shapes -- span attribute, metric, log record --
each queried the way that signal is meant to be queried, rather than forcing one
signal to do all three jobs.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from .semconv import Canon, Rosetta

logger = logging.getLogger("rosetta.emit")

#: Token-count buckets. The SDK default buckets top out around 10s-of-thousands
#: and are tuned for latency in milliseconds; a prompt can be a single token or
#: a million, so the defaults put almost every observation in one bucket and the
#: percentiles become meaningless.
TOKEN_BUCKETS = [
    0, 100, 500, 1_000, 2_500, 5_000, 10_000, 25_000,
    50_000, 100_000, 250_000, 500_000, 1_000_000,
]

#: Cost buckets in USD. Same reasoning: a single call ranges over five orders of
#: magnitude, from a cached haiku call to a long opus run.
COST_BUCKETS = [
    0.0, 0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0,
]


class RosettaMetrics:
    """Instruments recorded once per normalised GenAI span."""

    def __init__(self, meter: metrics.Meter) -> None:
        self.token_usage = meter.create_histogram(
            name="gen_ai.client.tokens",
            unit="{token}",
            description="Tokens used per GenAI operation (spec-defined instrument).",
        )
        # No GenAI convention defines a cost instrument, so this one is ours and
        # is namespaced accordingly rather than squatting on gen_ai.*.
        self.cost = meter.create_counter(
            name="rosetta.spend.usd",
            unit="USD",
            description="Computed cost of GenAI operations, normalised across frameworks.",
        )
        self.cost_distribution = meter.create_histogram(
            name="rosetta.spend.usd.distribution",
            unit="USD",
            description="Per-operation cost distribution, for percentile alerting.",
        )
        self.findings = meter.create_counter(
            name="rosetta.findings",
            unit="{finding}",
            description="Conformance and governance findings by code.",
        )
        self.dark_cost = meter.create_counter(
            name="rosetta.dark_cost.spans",
            unit="{span}",
            description="Inference spans carrying no token usage in any dialect.",
        )

    def record(
        self,
        attrs: Mapping[str, Any],
        *,
        cost_usd: float | None,
        is_aggregate: bool,
        finding_codes: tuple[str, ...] = (),
    ) -> None:
        """Record one span's measurements.

        Roll-up spans are skipped for cost and tokens: their children already
        reported the same tokens, and counting both doubles every total.
        """
        base = {
            "service.name": attrs.get("service.name") or "unknown_service",
            Canon.REQUEST_MODEL.value: attrs.get(Canon.REQUEST_MODEL.value) or "unknown",
            Canon.PROVIDER.value: attrs.get(Canon.PROVIDER.value) or "unknown",
            Rosetta.DIALECT.value: attrs.get(Rosetta.DIALECT.value) or "unknown",
        }

        if not is_aggregate:
            for canon, token_type in (
                (Canon.INPUT_TOKENS, "input"),
                (Canon.OUTPUT_TOKENS, "output"),
            ):
                value = attrs.get(canon.value)
                if isinstance(value, int) and value > 0:
                    # gen_ai.token.type is the dimension the spec defines.
                    self.token_usage.record(value, {**base, "gen_ai.token.type": token_type})

            if cost_usd:
                self.cost.add(cost_usd, base)
                self.cost_distribution.record(cost_usd, base)

        if attrs.get(Rosetta.DARK_COST.value):
            self.dark_cost.add(1, base)

        for code in finding_codes:
            self.findings.add(1, {**base, "rosetta.finding.code": code})


def build_meter_provider(
    *,
    service_name: str,
    endpoint: str,
    extra_resource: Mapping[str, Any] | None = None,
    export_interval_ms: int = 10_000,
) -> MeterProvider:
    """A MeterProvider exporting OTLP/HTTP with GenAI-appropriate buckets."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
    from opentelemetry.sdk.metrics.export import AggregationTemporality
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    attributes: dict[str, Any] = {"service.name": service_name}
    attributes.update(dict(extra_resource or {}))

    base = endpoint.rstrip("/")
    metrics_url = base if base.endswith("/v1/metrics") else f"{base}/v1/metrics"

    # Delta, not the SDK's default cumulative.
    #
    # An agent run is a short-lived process: it starts, emits, and exits. Each
    # process gets a fresh service.instance.id, so a cumulative counter produces
    # exactly one point per series and then the series ends. `rate` and
    # `increase` both need at least two points to compute a difference, so every
    # cost query comes back empty even though the data is plainly in storage.
    # With delta temporality each export carries the change since the last one,
    # which is directly summable across however many short-lived processes ran.
    preferred_temporality = {
        Counter: AggregationTemporality.DELTA,
        UpDownCounter: AggregationTemporality.DELTA,
        Histogram: AggregationTemporality.DELTA,
    }

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=metrics_url, preferred_temporality=preferred_temporality
        ),
        export_interval_millis=export_interval_ms,
    )
    return MeterProvider(
        resource=Resource.create(attributes),
        metric_readers=[reader],
        views=[
            View(
                instrument_name="gen_ai.client.tokens",
                aggregation=ExplicitBucketHistogramAggregation(TOKEN_BUCKETS),
            ),
            View(
                instrument_name="rosetta.spend.usd.distribution",
                aggregation=ExplicitBucketHistogramAggregation(COST_BUCKETS),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Governance logs
# ---------------------------------------------------------------------------


def build_logger_provider(
    *,
    service_name: str,
    endpoint: str,
    extra_resource: Mapping[str, Any] | None = None,
) -> Any:
    """A LoggerProvider exporting OTLP/HTTP, or None if unavailable.

    The OTel Python logs API has moved between `_logs` and `logs` across
    versions, so import failures degrade to "no logs" rather than taking the
    application down.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except Exception:  # pragma: no cover - depends on SDK version
        logger.debug("rosetta: OTel logs SDK unavailable; governance logs disabled")
        return None

    attributes: dict[str, Any] = {"service.name": service_name}
    attributes.update(dict(extra_resource or {}))

    base = endpoint.rstrip("/")
    logs_url = base if base.endswith("/v1/logs") else f"{base}/v1/logs"

    provider = LoggerProvider(resource=Resource.create(attributes))
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=logs_url))
    )
    return provider


#: Findings severe enough to deserve their own log record, with independent
#: retention from the trace that produced them.
GOVERNANCE_CODES: frozenset[str] = frozenset({"R001", "R004", "R012", "R014"})


def governance_message(
    code: str,
    attrs: Mapping[str, Any],
) -> tuple[str, str]:
    """Human-readable (severity, body) for a governance finding."""
    service = attrs.get("service.name") or "unknown_service"
    model = attrs.get(Canon.REQUEST_MODEL.value) or "unknown"
    if attrs.get(Rosetta.REDACTED.value):
        kinds = attrs.get(Rosetta.REDACTED_KINDS.value) or "unknown"
        return (
            "ERROR",
            f"credential leak: {service} sent {kinds} to {model} in a prompt; "
            "redacted before storage, rotate the affected secrets",
        )
    if code == "R001":
        return (
            "WARN",
            f"dark cost: {service} called {model} with no token usage reported; "
            "spend is invisible to every cost dashboard",
        )
    if code == "R012":
        return (
            "WARN",
            f"unpriced model: {service} spent tokens on {model}, which has no "
            "entry in the pricing table",
        )
    if code == "R014":
        return (
            "WARN",
            f"duplicate token reporting from {service} on {model}; a query "
            "summing both attribute names would double-count",
        )
    if code == "R004":
        return (
            "WARN",
            f"unqueryable telemetry: {service} reported token counts for {model} "
            "as a JSON string, which no backend can SUM; Rosetta unpacked them "
            "into gen_ai.usage.* so the numbers are numbers again",
        )
    return ("INFO", f"{code} on {service} ({model})")
