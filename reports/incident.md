# Rosetta incident report

- **Window:** last 20 minutes
- **Source:** SigNoz MCP (http://localhost:8000/mcp)
- **Tools used:** signoz_aggregate_traces, signoz_search_traces

## Verdict

ACTION REQUIRED -- credentials were transmitted to a model provider.

## Findings

### [CRITICAL] Were credentials sent to an LLM provider?

*via `signoz_aggregate_traces`*

```
Credentials reached an LLM provider. Rosetta redacted them before storage, but the calling application still transmitted them -- the keys must be rotated.
service.name=checkout-agent  value=4
```

### [WARNING] Is there spend nobody can see?

*via `signoz_aggregate_traces`*

```
Inference spans are reporting no token usage. This is real spend that no cost dashboard can attribute.
service.name=support-agent  value=12
service.name=billing-agent  value=12
service.name=research-agent  value=12
service.name=checkout-agent  value=12
```

### [WARNING] Are we spending on models with no price?

*via `signoz_aggregate_traces`*

```
Tokens spent against models missing from the pricing table; their cost is absent from every total.
gen_ai.request.model=acme-internal-reranker-v3  value=4
```

### [info] What did the fleet cost?

*via `signoz_aggregate_traces`*

```
service.name=checkout-agent  value=1.879
service.name=support-agent  value=0.649
service.name=billing-agent  value=0.142
service.name=research-agent  value=0.0293
```

### [info] Which services emit poor telemetry?

*via `signoz_aggregate_traces`*

```
service.name=checkout-agent  value=94.994
service.name=support-agent  value=83.652
service.name=research-agent  value=83.652
service.name=billing-agent  value=76.348
```

### [info] Is any agent stuck repeating a tool call?

*via `signoz_aggregate_traces`*

```
Tool-call distribution -- a single tool dominating one service is the runaway-loop signature.
gen_ai.tool.name=lookup_order  service.name=checkout-agent  value=108
gen_ai.tool.name=search_kb  service.name=support-agent  value=18
gen_ai.tool.name=search_kb  service.name=checkout-agent  value=16
gen_ai.tool.name=search_kb  service.name=billing-agent  value=15
gen_ai.tool.name=lookup_order  service.name=billing-agent  value=13
gen_ai.tool.name=search_kb  service.name=research-agent  value=13
gen_ai.tool.name=lookup_order  service.name=research-agent  value=12
gen_ai.tool.name=web_search  service.name=checkout-agent  value=10
gen_ai.tool.name=web_search  service.name=billing-agent  value=10
gen_ai.tool.name=web_search  service.name=research-agent  value=10
```

### [detail] Spans that carried credentials

*via `signoz_search_traces`*

```
service.name=checkout-agent  name=chat gpt-4o  trace_id=30ac10366c9754ca8eb8ab8335fcad89  deployment.environment=production  duration_nano=14238000  has_error=False
service.name=checkout-agent  name=chat gpt-4o  trace_id=e592e99272cf2858dc5312245540b9ee  deployment.environment=production  duration_nano=13267500  has_error=False
service.name=checkout-agent  name=chat gpt-4o  trace_id=7f576fcb65df1ea5a513cf8a0fbdda7d  deployment.environment=production  duration_nano=12843300  has_error=False
service.name=checkout-agent  name=chat gpt-4o  trace_id=f7ee141efca11aac649d3569e377e023  deployment.environment=production  duration_nano=13060100  has_error=False
service.name=checkout-agent  name=chat gpt-4o  trace_id=f24e90b66a470b3fd8c13a41af5b8273  deployment.environment=production  duration_nano=13253500  has_error=False
```
