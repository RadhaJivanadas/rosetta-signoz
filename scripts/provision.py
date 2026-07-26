"""Provision the Rosetta dashboard and alert rules in SigNoz, as code.

Everything here is created through the public API, so the observability surface
is reproducible rather than hand-clicked: ``python scripts/provision.py`` on a
fresh SigNoz produces the identical dashboard and alerts.

Schema notes, learned against a live v0.134.0 instance:

* Dashboards use the v2 API with the Perses-style ``schemaVersion: "v6"``.
  The v1 dashboard API is hard-deprecated and returns ``dashboard_deprecated``.
* With ``generateName: true`` the top-level ``name`` must be omitted; the server
  derives an RFC 1123 label. Supplying a human-readable ``name`` is rejected --
  the readable string belongs in ``spec.display.name``.
* Trace and log aggregations use the ``{"expression": "..."}`` string form.
  Metric aggregations use an object form instead; this dashboard is all traces.
* Alert rules post to ``/api/v2/rules`` with ``schemaVersion: "v2alpha1"``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Sequence

DEFAULT_BASE = os.environ.get("SIGNOZ_URL", "http://localhost:8080")


def _api_key() -> str:
    key = os.environ.get("SIGNOZ_API_KEY", "").strip()
    if key:
        return key
    for candidate in (".scratch/api_key.txt", "infra/api_key.txt"):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                found = handle.read().strip()
                if found:
                    return found
        except OSError:
            continue
    sys.exit("No API key. Set SIGNOZ_API_KEY or run scripts/bootstrap.py first.")


def call(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    base: str,
    key: str,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=data,
        headers={"Content-Type": "application/json", "SIGNOZ-API-KEY": key},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------


def trace_query(
    expression: str,
    *,
    name: str = "A",
    filter_expression: str | None = None,
    group_by: Sequence[str] = (),
    legend: str = "",
) -> dict[str, Any]:
    """A Query Builder v5 trace query in the shape a v6 panel expects."""
    spec: dict[str, Any] = {
        "signal": "traces",
        "name": name,
        "aggregations": [{"expression": expression}],
    }
    if filter_expression:
        spec["filter"] = {"expression": filter_expression}
    if group_by:
        spec["groupBy"] = [
            {
                "name": g,
                "fieldContext": "resource" if g.startswith("service.") else "attribute",
                "fieldDataType": "string",
                "signal": "traces",
            }
            for g in group_by
        ]
    if legend:
        spec["legend"] = legend
    return spec


def panel(
    *,
    title: str,
    description: str,
    expression: str,
    plugin: str = "signoz/TimeSeriesPanel",
    query_kind: str = "time_series",
    filter_expression: str | None = None,
    group_by: Sequence[str] = (),
    legend: str = "",
) -> dict[str, Any]:
    return {
        "kind": "Panel",
        "spec": {
            "display": {"name": title, "description": description},
            "plugin": {"kind": plugin, "spec": {"legend": {"position": "bottom"}}},
            "queries": [
                {
                    "kind": query_kind,
                    "spec": {
                        "name": "A",
                        "plugin": {
                            "kind": "signoz/BuilderQuery",
                            "spec": trace_query(
                                expression,
                                filter_expression=filter_expression,
                                group_by=group_by,
                                legend=legend,
                            ),
                        },
                    },
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# The dashboard
#
# Each panel answers a question that could not be asked before normalisation,
# because the underlying services share no attribute names.
# ---------------------------------------------------------------------------

PANELS: list[tuple[str, dict[str, Any]]] = [
    (
        "spend-by-service",
        panel(
            title="Agent spend by service (USD)",
            description=(
                "One query across four frameworks. No GenAI convention defines a "
                "cost attribute; rosetta.cost.usd is computed in the pipeline."
            ),
            expression="sum(rosetta.cost.usd)",
            group_by=("service.name",),
            legend="{{service.name}}",
        ),
    ),
    (
        "spend-by-model",
        panel(
            title="Spend by model",
            description="Model names normalised across OpenAI, Anthropic and self-hosted.",
            expression="sum(rosetta.cost.usd)",
            group_by=("gen_ai.request.model",),
            legend="{{gen_ai.request.model}}",
        ),
    ),
    (
        "tokens-by-service",
        panel(
            title="Input tokens by service",
            description=(
                "Includes billing-agent, whose counts arrived as a JSON string "
                "and were unqueryable as numbers until Rosetta unpacked them."
            ),
            expression="sum(gen_ai.usage.input_tokens)",
            group_by=("service.name",),
            legend="{{service.name}}",
        ),
    ),
    (
        "dark-cost",
        panel(
            title="Dark cost: inference spans with no token usage",
            description=(
                "Spend that is invisible to every cost dashboard. Container "
                "spans (agent/tool) are excluded -- they carry no tokens by design."
            ),
            expression="count()",
            filter_expression="rosetta.dark_cost = true",
            group_by=("service.name",),
            legend="{{service.name}}",
        ),
    ),
    (
        "secret-leaks",
        panel(
            title="Credentials redacted before storage",
            description=(
                "Prompts containing AWS keys, JWTs, private keys or connection "
                "strings. Redacted in the pipeline, so the secret never lands."
            ),
            expression="count()",
            filter_expression="rosetta.redacted = true",
            group_by=("service.name",),
            legend="{{service.name}}",
        ),
    ),
    (
        "conformance",
        panel(
            title="Telemetry conformance score by service",
            description=(
                "100 minus weighted findings. Ranks which teams emit portable "
                "telemetry and which are pinned to a vendor namespace."
            ),
            expression="avg(rosetta.conformance.score)",
            group_by=("service.name",),
            legend="{{service.name}}",
        ),
    ),
    (
        "unpriced",
        panel(
            title="Unpriced models",
            description=(
                "Real token spend against a model absent from the price table. "
                "Rosetta refuses to price these at zero, which would hide them."
            ),
            expression="count()",
            filter_expression="rosetta.unpriced_model = true",
            group_by=("gen_ai.request.model",),
            legend="{{gen_ai.request.model}}",
        ),
    ),
    (
        "dialects",
        panel(
            title="Spans by source dialect",
            description="Which vocabularies are actually in production right now.",
            expression="count()",
            group_by=("rosetta.dialect",),
            legend="{{rosetta.dialect}}",
        ),
    ),
]


def build_dashboard() -> dict[str, Any]:
    panels = {key: spec for key, spec in PANELS}
    # The Perses grid is 12 columns wide, not 24: an x of 12 is rejected
    # outright. Two columns of width 6 fill the row exactly.
    items = []
    for index, (key, _spec) in enumerate(PANELS):
        items.append(
            {
                "x": (index % 2) * 6,
                "y": (index // 2) * 7,
                "width": 6,
                "height": 7,
                "content": {"$ref": f"#/spec/panels/{key}"},
            }
        )
    return {
        "schemaVersion": "v6",
        "generateName": True,
        "tags": [{"key": "project", "value": "rosetta"}],
        "spec": {
            "display": {
                "name": "Rosetta -- polyglot agent telemetry",
                "description": (
                    "Cost, dark spend, leaked credentials and telemetry quality "
                    "across four agent frameworks that share no attribute names."
                ),
            },
            "variables": [],
            "links": [],
            "panels": panels,
            "layouts": [{"kind": "Grid", "spec": {"items": items}}],
        },
    }


# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------


#: Alert rules must reference an existing notification channel by name.
CHANNEL_NAME = "rosetta-webhook"

#: Where the demo's alert receiver listens. Nothing needs to be running for the
#: channel to be created; scripts/alert_sink.py can catch the payloads live.
CHANNEL_WEBHOOK_URL = os.environ.get(
    "ROSETTA_WEBHOOK_URL", "http://localhost:9099/alerts"
)


def ensure_channel(*, base: str, key: str) -> bool:
    """Create the notification channel unless it already exists.

    SigNoz rejects duplicate receiver names with ``alertmanager_config_conflict``
    rather than treating the create as idempotent, so existence is checked first.
    """
    status, payload = call("GET", "/api/v1/channels", None, base=base, key=key)
    if status == 200 and isinstance(payload, dict):
        for channel in payload.get("data") or []:
            if channel.get("name") == CHANNEL_NAME:
                print(f"[skip] channel '{CHANNEL_NAME}' already exists")
                return True

    body = {
        "name": CHANNEL_NAME,
        "webhook_configs": [{"url": CHANNEL_WEBHOOK_URL, "send_resolved": True}],
    }
    status, payload = call("POST", "/api/v1/channels", body, base=base, key=key)
    if status in (200, 201):
        print(f"[ok]   channel '{CHANNEL_NAME}' -> {CHANNEL_WEBHOOK_URL}")
        return True
    print(f"[FAIL] channel: HTTP {status}: {str(payload)[:300]}")
    return False


def existing_alerts(*, base: str, key: str) -> dict[str, str]:
    """Map of configured rule name -> rule id, so re-runs do not duplicate."""
    for path in ("/api/v1/rules", "/api/v2/rules"):
        status, payload = call("GET", path, None, base=base, key=key)
        if status != 200 or not isinstance(payload, dict):
            continue
        data = payload.get("data")
        rules = data.get("rules") if isinstance(data, dict) else data
        found: dict[str, str] = {}
        for rule in rules or []:
            if not isinstance(rule, dict):
                continue
            name = rule.get("alert") or (rule.get("data") or {}).get("alert")
            rule_id = rule.get("id") or rule.get("uuid")
            if name:
                found[name] = str(rule_id) if rule_id is not None else ""
        if found:
            return found
    return {}


def alert_rule(
    *,
    name: str,
    description: str,
    expression: str,
    threshold: float,
    filter_expression: str | None = None,
    severity: str = "warning",
    op: str = "above",
    eval_window: str = "30m",
) -> dict[str, Any]:
    """Build a threshold rule over trace spans.

    ``eval_window`` defaults to 30 minutes rather than the more usual 5. These
    conditions are *rare discrete events* -- one leaked credential, one call to
    an unpriced model -- not continuous rates. With a 5m rolling window the
    single matching span slides out of range within a couple of evaluation
    cycles and the alert silently resolves itself, which for a leaked AWS key is
    precisely the wrong behaviour: the secret was still sent, and the operator
    still has to rotate it.
    """
    spec: dict[str, Any] = {
        "name": "A",
        "signal": "traces",
        "stepInterval": 60,
        "aggregations": [{"expression": expression}],
    }
    if filter_expression:
        spec["filter"] = {"expression": filter_expression}

    return {
        "alert": name,
        "alertType": "TRACES_BASED_ALERT",
        "ruleType": "threshold_rule",
        "version": "v5",
        "schemaVersion": "v2alpha1",
        "condition": {
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "queries": [{"type": "builder_query", "spec": spec}],
            },
            "selectedQueryName": "A",
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": severity,
                        "op": op,
                        "matchType": "at_least_once",
                        "target": threshold,
                        # A rule with no channel is rejected, and the channel
                        # must already exist -- see ensure_channel().
                        "channels": [CHANNEL_NAME],
                    }
                ],
            },
        },
        "evaluation": {
            "kind": "rolling",
            "spec": {"evalWindow": eval_window, "frequency": "1m"},
        },
        # Mandatory under schemaVersion v2alpha1 -- the rule is rejected without
        # it. Grouping by service keeps one noisy agent from suppressing alerts
        # for the rest of the fleet.
        "notificationSettings": {
            "groupBy": ["service.name"],
            "renotify": {
                "enabled": True,
                "interval": "4h",
                "alertStates": ["firing"],
            },
        },
        "labels": {"severity": severity, "project": "rosetta"},
        "annotations": {"summary": description},
    }


ALERTS: list[dict[str, Any]] = [
    alert_rule(
        name="Rosetta: credentials leaked into prompts",
        description=(
            "A prompt contained an AWS key, JWT, private key or connection string. "
            "Rosetta redacted it before storage, but the source application is "
            "still sending secrets to its LLM provider."
        ),
        expression="count()",
        filter_expression="rosetta.redacted = true",
        threshold=0,
        severity="critical",
    ),
    alert_rule(
        name="Rosetta: dark cost detected",
        description=(
            "Inference spans are reporting no token usage. This is real spend "
            "that no cost dashboard can attribute -- usually a streaming response "
            "whose usage block is never read, or a proxy stripping it."
        ),
        expression="count()",
        filter_expression="rosetta.dark_cost = true",
        threshold=5,
        severity="warning",
    ),
    alert_rule(
        # A rate, not a discrete event, so a short window is the right one.
        name="Rosetta: agent spend spike",
        description=(
            "Total normalised agent spend crossed the budget for the window. "
            "Works across every framework because cost is canonical."
        ),
        expression="sum(rosetta.cost.usd)",
        threshold=0.5,
        severity="warning",
        eval_window="5m",
    ),
    alert_rule(
        name="Rosetta: telemetry conformance dropped",
        description=(
            "A service is emitting low-quality GenAI telemetry -- vendor-locked "
            "attributes, legacy names, or values trapped in JSON strings."
        ),
        expression="avg(rosetta.conformance.score)",
        threshold=80,
        severity="warning",
        op="below",
        eval_window="15m",
    ),
    alert_rule(
        name="Rosetta: unpriced model in use",
        description=(
            "Tokens are being spent against a model with no entry in the pricing "
            "table, so its cost is silently missing from every spend total."
        ),
        expression="count()",
        filter_expression="rosetta.unpriced_model = true",
        threshold=0,
        severity="warning",
    ),
]


# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-alerts", action="store_true")
    parser.add_argument("--skip-dashboard", action="store_true")
    parser.add_argument(
        "--replace-alerts",
        action="store_true",
        help="delete and recreate alert rules instead of skipping existing ones",
    )
    args = parser.parse_args(argv)

    key = _api_key()
    failures = 0

    if not args.skip_dashboard:
        dashboard = build_dashboard()
        if args.dry_run:
            print(json.dumps(dashboard, indent=2)[:4000])
        else:
            status, payload = call(
                "POST", "/api/v2/dashboards", dashboard, base=args.base, key=key
            )
            if status in (200, 201):
                data = (payload or {}).get("data", {})
                print(
                    f"[ok]   dashboard '{data.get('name')}' "
                    f"({len(PANELS)} panels) -> {args.base}/dashboard/{data.get('id')}"
                )
            else:
                failures += 1
                print(f"[FAIL] dashboard: HTTP {status}: {str(payload)[:400]}")

    if not args.skip_alerts:
        if not args.dry_run and not ensure_channel(base=args.base, key=key):
            return 1
        already = {} if args.dry_run else existing_alerts(base=args.base, key=key)
        for rule in ALERTS:
            if args.dry_run:
                print(json.dumps(rule, indent=2)[:1500])
                continue
            name = rule["alert"]
            if name in already:
                if not args.replace_alerts:
                    print(f"[skip] alert '{name}' already exists")
                    continue
                rule_id = already[name]
                if rule_id:
                    call("DELETE", f"/api/v1/rules/{rule_id}", None, base=args.base, key=key)
            status, payload = call("POST", "/api/v2/rules", rule, base=args.base, key=key)
            if status in (200, 201):
                print(f"[ok]   alert '{rule['alert']}'")
            else:
                failures += 1
                print(f"[FAIL] alert '{rule['alert']}': HTTP {status}: {str(payload)[:300]}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
