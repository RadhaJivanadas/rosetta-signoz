# Rosetta — submission video script

Target: **under 3:00**. Required order: **About the project → Tech stack and
architecture → Live demo**.

## 0:00 — About the project

Rosetta makes polyglot AI-agent telemetry queryable. Four frameworks report the
same token usage in incompatible fields, and OpenTelemetry defines no cost
field. Rosetta normalises them before SigNoz, so one query shows cost across the
fleet.

## 0:18 — Tech stack and architecture

Python and OpenTelemetry power one processor: redact, normalise, price, and
guard. OTLP sends traces, metrics, and governance logs to SigNoz. Foundry
reproduces the full stack. Canonical attributes drive dashboards, alerts, a
circuit breaker, and an MCP investigator.

## 0:43 — Live demo

The live demo shows:

1. Langfuse token counts trapped in a JSON string.
2. A seeded four-framework fleet producing canonical cost and token usage.
3. One SigNoz dashboard query across every service.
4. Credentials redacted before storage.
5. Alerts firing on normalised findings.
6. A circuit breaker stopping a repeated-tool loop.
7. A SigNoz MCP investigator producing an actionable incident verdict.

## Close

The GenAI conventions are still changing. Rosetta performs the translation in
the OpenTelemetry pipeline, so the telemetry remains vendor-neutral and the
same controls work across the whole agent fleet.
