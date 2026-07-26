# Demo script — 3 minutes

Judging weights presentation, so this is built around one reveal rather than a
feature tour. Total runtime ~3:00. Record at 1080p; keep the SigNoz UI at
default zoom so panel text stays legible.

**Before recording:** run `python demo/fleet.py` twice, a few minutes apart, so
the dashboard has more than a single point and the alerts have evaluated.

---

## 0:00–0:25 · The setup

> "Four services. Four agent frameworks. This is an ordinary company."

Show `demo/fleet.py`'s dialect table on screen — the four emitters side by side.

> "Pydantic AI says `gen_ai.usage.input_tokens`. CrewAI says
> `llm.token_count.prompt`. LangChain says `gen_ai.usage.prompt_tokens`.
> And Langfuse says…"

Highlight the Langfuse line:

```python
"langfuse.observation.usage_details": '{"input": 1200, "output": 350}'
```

> "…a JSON string. That's not a number. You cannot SUM it."

---

## 0:25–0:55 · The failure

Open SigNoz → Traces. Filter `service.name = billing-agent`. Open a span.

> "The data is here. The token count is here. But it's text inside a blob."

Now try the question that matters. Traces → aggregate `sum(gen_ai.usage.input_tokens)`
grouped by `service.name`, **with Rosetta disabled** (`ROSETTA_REDACT=0` is not
enough — run `git stash` on the processor, or simply narrate):

> "Three of four services return nothing. And *none* of them report cost —
> because the OpenTelemetry GenAI spec doesn't define a cost attribute at all.
> Not one. There is no way to ask what my agents cost."

---

## 0:55–1:35 · The turn

> "Rosetta is one span processor. Redact, normalise, price."

Run it live:

```bash
python demo/fleet.py
```

Let the summary land on screen. Then the same query, now:

```
sum(rosetta.cost.usd) by service.name
    checkout-agent  0.479    support-agent  0.196
    billing-agent   0.037    research-agent 0.008

sum(gen_ai.usage.input_tokens) by service.name
    checkout-agent  187446   support-agent  35923
    research-agent  29266    billing-agent  25504
```

> "One query. Four frameworks. Including billing-agent, whose numbers were a
> string sixty seconds ago. And dollars — which nobody reported."

**This is the moment. Pause here.**

---

## 1:35–2:20 · What was hiding

Open the provisioned dashboard. Walk three panels, fast:

**Dark cost — 12 spans.**
> "Inference calls that reported no tokens at all. Streaming responses whose
> usage block was never read. Real money, invisible to every cost dashboard.
> Found with `NOT EXISTS` — absence as a predicate."

**Credentials redacted — 1 span, 6 secrets.**
> "An AWS key, a secret, a JWT, a database URL with a password, a card number,
> an email. Someone pasted a broken config into a prompt. Rosetta destroyed
> them *before* storage — not at query time, because by then they're at rest."

Open the span to show `[REDACTED:aws_access_key_id]`.

> "The kind is kept. You still know an AWS key was leaked. You just can't read it."

**Conformance score.**
> "95 for Pydantic AI. 76 for Langfuse. That's a ranking of which teams emit
> telemetry you can actually query."

Then Alerts: four firing.

> "These fire on normalised attributes, so one rule covers every framework."

---

## 2:20–2:50 · Agents watching agents

```bash
python scripts/investigate.py
```

> "This is the hackathon's premise, literally. An agent reads the fleet's
> telemetry back through SigNoz's MCP server — the same tools an operator has —
> and writes the postmortem."

Show the verdict line:

```
ACTION REQUIRED -- credentials were transmitted to a model provider.
```

> "No LLM API key. Deterministic. Same report every run."

---

## 2:50–3:00 · Close

> "The GenAI conventions aren't stable — every attribute is still
> `development`, they moved repositories, and they have no released version.
> Until they settle, somebody has to do this translation.
>
> A vendor backend hides it by rewriting your data on ingest. SigNoz stores
> what you send, which is the honest behaviour — so Rosetta does it in the
> pipeline, and your telemetry stays yours."

---

## Things to have ready

- Terminal with the venv active, `PYTHONPATH` set.
- SigNoz on `localhost:8080`, logged in, dashboard open in a second tab.
- Run `python demo/fleet.py` once *before* recording so panels aren't empty.

## Do not

- Do not tour the code. One reveal beats seven features.
- Do not claim nobody computes cost — OpenLIT, Traceloop and Pydantic AI do,
  SDK-side. The accurate claim is that the *spec* defines none, four SDKs
  disagree on where it lives, and OpenInference/Strands/ADK emit none at all.
- Do not skip the Langfuse JSON-string moment. It is the most concrete,
  least arguable illustration of the whole problem.
