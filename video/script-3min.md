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

1. Logging in to the live self-hosted SigNoz instance.
2. Four services instrumented by four different frameworks.
3. A current numeric OpenTelemetry token field.
4. Langfuse token counts trapped in a JSON string.
5. The Rosetta processor running the seeded fleet.
6. One SigNoz dashboard query covering every service on one chart.

The demo portion is the uninterrupted first `2:18.55` of the longer source
recording, ending after the word “chart.”
