# Rosetta: one vocabulary for polyglot AI-agent telemetry

**Track 01, AI & Agent Observability** · *Agents of SigNoz* hackathon

[Read the build article](https://radhajivanadas.github.io/rosetta-signoz/) ·
[Watch the 3-minute submission video](https://github.com/RadhaJivanadas/rosetta-signoz/releases/download/v1.0.0/rosetta-submission.mp4)

> Four agent services. Four instrumentation libraries. Zero shared attribute
> names. Ask *"what did my agents cost this hour"* and no single query can
> answer it.
>
> Rosetta makes that one query work, and it finds the spend, the leaked
> credentials and the broken telemetry nobody was looking at.

---

## The problem is real, current, and verifiable

The OpenTelemetry GenAI semantic conventions are **not stable**, and the
ecosystem never converged on them.

| Claim | Evidence |
|---|---|
| Nothing in `gen_ai.*` is stable | Every attribute in the registry is `development`; zero `stable` |
| The conventions moved and have no release | Split out of core semconv at **v1.42.0** into `semantic-conventions-genai`, which has **no tagged release** and a literal `TODO` schema URL |
| Names churn underneath you | `gen_ai.system` → `gen_ai.provider.name` (v1.37.0); `prompt_tokens` → `input_tokens` (v1.27.0) |
| **There is no cost attribute at all** | The spec defines token histograms and no USD anywhere |

So four libraries in wide production use report the same number four ways:

| Framework | Reports input tokens as | Queryable as a number? |
|---|---|---|
| Pydantic AI / Logfire | `gen_ai.usage.input_tokens` | yes |
| CrewAI / OpenInference | `llm.token_count.prompt` | yes, wrong name |
| LangChain / OpenLLMetry | `gen_ai.usage.prompt_tokens` | yes, superseded name |
| Langfuse SDK v4 | `langfuse.observation.usage_details` = `'{"input": 1200}'` | **no, it is a string** |

A vendor backend hides this by normalising on ingest (Langfuse documents
precedence across four conventions). **An OTLP-native backend like SigNoz
stores exactly what it receives**, which is the correct, no-lock-in behaviour,
and precisely why the fragmentation lands on the user.

Rosetta fixes it in the pipeline instead of the backend, so the result stays
vendor-neutral.

> **Prior art, stated honestly.** OpenLIT, Traceloop and Pydantic AI *do*
> compute USD SDK-side, so "nobody has cost" would be an overclaim. The gap is
> that OpenInference, Strands, ADK and Microsoft Agent Framework emit none, the
> spec defines none, and the four SDKs that do compute it disagree on where it
> lives. SigNoz itself has `pkg/types/llmpricingruletypes` in-tree and an
> `enable_ai_observability` flag, with the pricing processors still unmerged, so
> Rosetta complements that work from the SDK side rather than duplicating it.

---

## What Rosetta does

A single OpenTelemetry `SpanProcessor`. Three passes, in a fixed order:

1. **Redact.** Secrets are destroyed *before* anything else reads or copies
   the content. Not at query time; by then the secret is already at rest.
2. **Normalise.** Seven dialects resolve into one canonical vocabulary,
   first-match-wins over an explicit precedence list. **Additive**: source
   attributes are never deleted, so existing dashboards keep working.
3. **Price.** USD computed from model × tokens, with cache-read/write and
   reasoning tokens billed at their own rates.

Then the same finding is emitted in **three signal shapes**, each queried the
way that signal is meant to be:

- **span attribute**, for drill-down in a trace
- **metric**, pre-aggregated and cheap to alert on at production cardinality
- **log record**, so governance findings outlive their trace. A security
  finding that expires with a sampled span is not a security control.

---

## Quickstart

```bash
# 1. SigNoz + MCP server, one config (casting.yaml is in this repo)
cd infra && foundryctl cast -f casting.yaml

# 2. Admin user, service account, admin role, API key. Scripted and idempotent
python scripts/bootstrap.py

# 3. Emit the polyglot fleet (no LLM API keys needed, seeded, reproducible)
python demo/fleet.py

# 4. Alert rules + notification channel, as code
python scripts/provision.py

# 5. The dashboard, in the schema this SigNoz version renders
python scripts/provision_dashboard.py --replace

# 6. An agent investigates the agents, over MCP
python scripts/investigate.py --out reports/incident.md
```

> **Foundry note.** `foundryctl cast` enforces a hard **5-minute** timeout on
> the deploy step. On a cold image pull it is killed mid-download and reports
> `exit status 1` although the config is fine. `forge` has already written
> `pours/` and `casting.yaml.lock` by then, so finish with:
> `docker compose -f pours/deployment/compose.yaml up -d`.

Both `casting.yaml` and `casting.yaml.lock` are committed, per the rules, so
the exact deployment can be reproduced.

---

## The demo

`demo/fleet.py` runs four services, each emitting a different dialect, with
four pathologies planted. Everything is seeded, so the numbers below reproduce exactly.

```
$ python demo/fleet.py

  checkout-agent   Pydantic AI                genai=  90 priced=  37 dark=  3 redacted= 1 $0.478870
  support-agent    CrewAI / OpenInference     genai=  39 priced=  12 dark=  3 redacted= 0 $0.196464
  research-agent   LangChain / OpenLLMetry    genai=  39 priced=  12 dark=  3 redacted= 0 $0.007975
  billing-agent    Langfuse SDK v4            genai=  39 priced=  12 dark=  3 redacted= 0 $0.037043

  TOTAL                                       genai= 207 priced=  73 dark= 12 redacted= 1 $0.720353
```

### The query that was impossible

```
sum(rosetta.cost.usd) by service.name
    checkout-agent  0.479      support-agent  0.196
    billing-agent   0.037      research-agent 0.008

sum(gen_ai.usage.input_tokens) by service.name
    checkout-agent  187446     support-agent  35923
    research-agent  29266      billing-agent  25504
```

`billing-agent`'s tokens arrived as a JSON **string**. None of the four
reported a cost. One query now covers all of them.

### What was hiding in there

| Finding | Result |
|---|---|
| **Dark cost**: inference spans with no token usage | 12 spans, 3 per service |
| **Leaked credentials**: AWS key, secret, JWT, DB URL, card, email | 6 secrets across 6 detectors, redacted pre-storage |
| **Unpriced model**: real spend, no price entry | `acme-internal-reranker-v3` |
| **Runaway loop**: one tool, one argument, 24 turns, context growing each turn | visible as tool-call concentration |
| **Conformance score** | Pydantic AI **95** · OpenInference/OpenLLMetry **84** · Langfuse **76** |

---

## Findings catalogue

| Code | Severity | Meaning |
|---|---|---|
| `R001` | error | **Dark cost**: inference span reports no tokens in any dialect |
| `R002` | warning | Resolved from a superseded spec name |
| `R003` | warning | Resolved from a vendor-proprietary namespace |
| `R004` | error | Numeric value trapped inside a JSON string |
| `R005` | info | Dual emission (`gen_ai.system` *and* `gen_ai.provider.name`) |
| `R006` | warning | No `gen_ai.operation.name`, not inferable |
| `R007` | info | Operation name not in the spec's value set |
| `R008` | warning | No model: cannot price, cannot group |
| `R009` | info | Aggregate roll-up usage; exclude from `SUM` |
| `R010` | warning | `service.name` missing: spend is unattributable |
| `R012` | warning | Model absent from the pricing table |
| `R013` | info | Self-hosted; priced zero by policy, not ignorance |
| `R014` | error/warning | Same quantity reported twice: **double-counting risk** |

`R014` is not theoretical: AWS Strands emits `prompt_tokens` **and**
`input_tokens` on the same span. Resolution picks one, correctly. But staying
silent about the duplicate would leave anyone hand-writing a query
double-counting with no way to know.

---

## Enforcement, and why it belongs here

Observation is not ownership. An agent you can only *watch* burn money is still
not one you own, so `rosetta/guard.py` closes the loop: a per-run circuit
breaker on **budget**, on **tool loops** (same tool, same arguments, N times),
and on **context runaway** (input tokens growing every turn, which is how a run
looks when it keeps appending its own failures to the prompt).

```
circuit breaker  budget=$0.35/run, max 8 identical tool calls
  checkout-agent: stopped the loop after 7/24 turns, avoiding ~$0.2886
  [R022] prompt grew on 6 consecutive calls, cost compounds every turn
```

The reason it lives in Rosetta rather than in a framework is the point of the
whole project: **a budget rule needs to know what a run cost, and in a polyglot
fleet that number does not exist until something normalises it.** Written
against Pydantic AI's attribute names, the same breaker would have guarded one
service out of four. `tests/test_guard.py` proves it: one run whose spans arrive in three
different vocabularies still accumulates a single budget.

Enforcement is cooperative by construction: nothing outside an agent's own loop
can abort a model call already in flight, so the agent asks (`guard.check()`)
and the breaker answers. Its decisions land on the span as
`rosetta.guard.tripped`, because a guardrail you cannot audit is just an outage
with extra steps.

Codes: `R020` budget · `R021` tool loop · `R022` context runaway.

---

## Three decisions that make the numbers trustworthy

**Never invent a price.** An unknown model yields `None`, not `0`. Pricing an
unknown model at zero turns real spend invisible. That is the exact failure
this project exists to expose.

**Never double-count.** Pydantic AI's `gen_ai.aggregated_usage.*` is a roll-up
of its children. Reading both it and the child usage doubles every token and
every dollar. Rosetta treats aggregates as a fallback only and flags them so
aggregation queries can exclude them.

**Never confuse two diagnoses.** A priceable model with no tokens is *dark
cost*, not an *unpriced model*. Conflating them made the "which models can't I
price" panel list `gpt-4o`, which prices fine.

---

## Depth of SigNoz usage

| Signal | How it is used |
|---|---|
| **Traces** | Canonical `gen_ai.*` + `rosetta.*` attributes on every span |
| **Metrics** | `rosetta.spend.usd`, `gen_ai.client.tokens` histogram, `rosetta.findings`, `rosetta.dark_cost.spans`; custom buckets, delta temporality |
| **Logs** | Governance findings as structured records, searchable by `body REGEXP` |
| **Dashboards** | 8 panels created through the API, as code (see the version note below) |
| **Alerts** | 5 threshold rules + notification channel via `/api/v2/rules` |
| **Query Builder** | `EXISTS`/`NOT EXISTS`, boolean filters, regex on log bodies, cross-signal grouping |
| **MCP** | `scripts/investigate.py` drives `signoz_aggregate_traces`, `signoz_search_traces`, `signoz_query_metrics`, `signoz_search_logs` |
| **Foundry** | `casting.yaml` + `.lock` committed; MCP enabled declaratively |

Query Builder capabilities exercised map directly onto SigNoz's own
showcase issue [#11674](https://github.com/SigNoz/signoz/issues/11674):
absence-as-a-predicate finds dark cost (`gen_ai.usage.input_tokens NOT EXISTS`),
and regex over log bodies finds leaked secrets.

---

## Two bugs worth reading about

Both were found by testing against reality rather than against my own
assumptions, and both are the kind that pass every unit test.

**Enrichment never reached the backend.** A finished span's `attributes` is a
`BoundedAttributes` built with `immutable=True`. Per-key assignment raises, my
`except` swallowed it at debug level, the processor's counters reported correct
totals, and *nothing* arrived in SigNoz. Only `test_enrichment_reaches_the_exporter`,
which asserts on what the **exporter** received, catches this.

**The checker reported "all clear" while broken.** The MCP investigator's
interpreter decided "no findings" from the absence of digits. When a tool call
failed validation, the error text contained digits, so it cheerfully reported
*"No redacted credentials"* for a query that never ran. It now distinguishes
"nothing found" from "nothing asked", and says `unknown` rather than `ok`.

**A 201 is not proof anyone can see the result.** `POST /api/v2/dashboards`
accepts a Perses `schemaVersion: "v6"` document on v0.134.0, stores it
faithfully. Panels, layouts and queries all survive a round trip. The
bundled frontend then renders *"Welcome to your new dashboard"*, empty. The
browser's own network log gives it away: the dashboard list calls
`GET /api/v1/dashboards`, which returns the v2 document shape, and the
v0.134.0 UI is looking for `title` / `widgets` / `layout`. Backend ahead of
frontend. `scripts/provision_dashboard.py` writes the v1 widget shape, which
this version actually draws; revisit after v0.135.0.

I only found this because I tried to screenshot the dashboard for the demo.
Every API call had returned success.

Also worth knowing: **metric temporality metadata is sticky per metric name in
SigNoz.** Cumulative counters from short-lived processes produce one point per
series, so `rate`/`increase` return empty. Delta temporality fixes it, but only
under a metric name that has not already been ingested as cumulative.

---

## Layout

```
rosetta/
  semconv.py     seven dialects, canonical vocabulary, precedence
  normalize.py   resolution engine, conformance scoring, findings
  pricing.py     USD computation; refuses to guess
  pricing.yaml   rates (INDICATIVE: read the header before trusting a number)
  redact.py      secret/PII detection; Luhn-validated cards
  guard.py       budget / loop / runaway circuit breaker
  emit.py        metrics + governance logs
  processor.py   the SpanProcessor that ties it together
demo/fleet.py       four dialects, five planted pathologies, seeded
scripts/
  bootstrap.py            SigNoz setup, idempotent
  provision.py            alert rules + notification channel
  provision_dashboard.py  the 8-panel dashboard
  query.py                the cross-framework queries
  investigate.py          agent postmortem over MCP
tests/           28 tests
infra/           casting.yaml + casting.yaml.lock
```

---

## Limitations

- **Pricing is indicative, not billing-grade.** `pricing.yaml` rates were
  captured for this demo and are stale by the time you read this. Point
  `ROSETTA_PRICING_FILE` at your negotiated rates. Use the output to *detect
  anomalies*, not to invoice.
- **Dialects are hand-maintained.** Seven are covered. A new framework needs a
  new `Dialect` entry. Small, but not automatic.
- **Redaction is conservative by design.** Only structurally unambiguous or
  validated patterns. It will miss a secret with no distinctive shape; a false
  positive silently destroys debugging content, so precision was preferred.
- **The demo fleet is simulated.** Token counts come from a seeded RNG so the
  run needs no API keys and reproduces exactly. The attribute names, dialect
  shapes and pathologies are all real.
- **Not yet a Collector processor.** Rosetta runs in-process. The same logic as
  an OTTL/Collector component would cover non-Python fleets; that is the
  natural next step.

---

## AI assistant disclosure

Per the hackathon rules: AI coding assistants were used for research,
implementation, testing, video production, and documentation. All work was
directed and reviewed by the submitter, and the reported behaviour was verified
against a live self-hosted SigNoz v0.134.0 instance.

## License

MIT.
