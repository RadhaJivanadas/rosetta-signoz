# Agents of SigNoz submission

Copy-ready form answers.

## Email

`kovyrus@gmail.com`

## Team name

`Ruslan Kovychev`

## Name of the person submitting the form

`Ruslan Kovychev`

## Track

`Track 1: AI & Agent Observability`

## Project description

Rosetta is an OpenTelemetry pipeline layer that makes telemetry from polyglot
AI-agent frameworks queryable through one canonical vocabulary. It normalises
seven GenAI telemetry dialects, calculates model cost, redacts secrets before
storage, detects dark and duplicate usage, and applies a cross-framework
runtime circuit breaker. A reproducible Foundry deployment sends traces,
metrics, and structured governance logs to SigNoz, where provisioned dashboards
and alerts cover the full agent fleet. A SigNoz MCP investigator then queries
the same telemetry and produces an actionable incident report.

## GitHub link

https://github.com/RadhaJivanadas/rosetta-signoz

The required files are included:

- `infra/casting.yaml`
- `infra/casting.yaml.lock`

## Deployed link

Leave blank. The complete SigNoz deployment is a stateful multi-service Foundry
stack and is reproducible locally from the repository.

## YouTube video demo link

Add the YouTube URL after uploading:

`[YOUTUBE URL]`

Temporary direct video URL:

https://github.com/RadhaJivanadas/rosetta-signoz/releases/download/v1.0.0/rosetta-submission.mp4

## Describe how you have used SigNoz

Rosetta uses SigNoz as the shared observability and investigation plane for a
seeded fleet of four AI-agent services instrumented by four different
frameworks. The project sends canonical traces, delta-temporality metrics, and
structured governance logs over OTLP. SigNoz Query Builder proves that cost and
token usage can be aggregated across incompatible source dialects with one
query. An eight-panel dashboard visualises spend, tokens, dark cost, redacted
credentials, unpriced models, conformance, and source dialects. Five alert
rules fire on canonical findings, including leaked credentials and dark cost.
Finally, the SigNoz MCP server lets an investigator agent query the same live
telemetry and write an incident report. SigNoz and its MCP server are installed
with Foundry, and the exact casting files are committed for reproduction.

## Project blog link

https://radhajivanadas.github.io/rosetta-signoz/

## How was your hackathon experience?

The hackathon turned an apparently simple dashboard question into a deeper
OpenTelemetry investigation. The most valuable part was testing every layer
against a live SigNoz instance rather than stopping at unit tests. That exposed
two failures I would otherwise have missed: completed span attributes could not
be mutated before export, and browser automation was failing because its
telemetry had aged outside the selected time window. Building traces, metrics,
logs, dashboards, alerts, enforcement, and an MCP investigation around one
problem made the project feel like a real operational system rather than a
standalone demo. I leave with a much stronger understanding of OpenTelemetry
data flow, SigNoz APIs, and the practical cost of inconsistent GenAI semantic
conventions.

## YouTube metadata

Title:

`Rosetta: One Vocabulary for Polyglot AI-Agent Telemetry | Agents of SigNoz`

Description:

```text
Rosetta normalises fragmented OpenTelemetry GenAI telemetry so one SigNoz
query, dashboard, alert, and runtime guard works across Pydantic AI, CrewAI,
LangChain, and Langfuse.

Project: https://github.com/RadhaJivanadas/rosetta-signoz
Build article: https://radhajivanadas.github.io/rosetta-signoz/

Agents of SigNoz — Track 1: AI & Agent Observability
```
