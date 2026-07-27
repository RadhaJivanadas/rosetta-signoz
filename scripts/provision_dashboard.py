"""Provision the Rosetta dashboard in the schema this SigNoz version renders.

Why this exists alongside the v2 payload in ``provision.py``
-----------------------------------------------------------
SigNoz v0.134.0 accepts a Perses-style ``schemaVersion: "v6"`` dashboard on
``POST /api/v2/dashboards`` and returns 201. It stores it faithfully -- panels,
layouts and queries all survive a round trip. **The bundled frontend then
refuses to render it.**

The reason is visible in the browser's own network traffic: the dashboard list
page calls ``GET /api/v1/dashboards``, and that endpoint hands back the v2
document shape (``metadata`` / ``spec``). The v0.134.0 UI reads ``title`` /
``widgets`` / ``layout``, finds none of them, and falls back to the empty
"Welcome to your new dashboard" state. The data is there; the UI of this
version cannot see it.

So the backend is ahead of the frontend, and a 201 from the v2 API is not
evidence that anyone can look at the result. This module writes the v1 widget
shape instead, which this version renders. Re-check when upgrading past
v0.135.0, where the v2 UI lands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any, Sequence

DEFAULT_BASE = os.environ.get("SIGNOZ_URL", "http://localhost:8080")


def _api_key() -> str:
    key = os.environ.get("SIGNOZ_API_KEY", "").strip()
    if key:
        return key
    for candidate in ("infra/api_key.txt", ".scratch/api_key.txt"):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                found = handle.read().strip()
                if found:
                    return found
        except OSError:
            continue
    sys.exit("No API key. Set SIGNOZ_API_KEY or run scripts/bootstrap.py first.")


def call(method: str, path: str, body: Any, *, base: str, key: str):
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
# v1 builder-query helpers
# ---------------------------------------------------------------------------


def attribute(key: str, data_type: str = "float64", kind: str = "tag") -> dict:
    return {"key": key, "dataType": data_type, "type": kind, "isColumn": False}


def group_by(key: str) -> dict:
    # Resource-scoped keys must say so, or the grouping silently returns nothing.
    kind = "resource" if key.startswith("service.") else "tag"
    return {"key": key, "dataType": "string", "type": kind, "isColumn": False}


def bool_filter(key: str) -> dict:
    return {
        "op": "AND",
        "items": [
            {
                "id": str(uuid.uuid4())[:8],
                "key": attribute(key, "bool", "tag"),
                "op": "=",
                "value": True,
            }
        ],
    }


def widget(
    *,
    title: str,
    description: str,
    operator: str,
    attribute_key: str | None,
    group: Sequence[str],
    panel: str = "graph",
    filters: dict | None = None,
    legend: str = "",
) -> dict:
    query_data = {
        "dataSource": "traces",
        "queryName": "A",
        "aggregateOperator": operator,
        "aggregateAttribute": attribute(attribute_key) if attribute_key else attribute(""),
        "filters": filters or {"op": "AND", "items": []},
        "groupBy": [group_by(g) for g in group],
        "expression": "A",
        "disabled": False,
        "legend": legend,
        "stepInterval": 60,
        "reduceTo": "sum",
        "having": [],
        "limit": None,
        "orderBy": [],
    }
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "description": description,
        "panelTypes": panel,
        "query": {
            "queryType": "builder",
            "builder": {"queryData": [query_data], "queryFormulas": []},
        },
    }


WIDGETS: list[dict] = [
    widget(
        title="Agent spend by service (USD)",
        description=(
            "One query across four frameworks. No GenAI convention defines a cost "
            "attribute; rosetta.cost.usd is computed in the pipeline."
        ),
        operator="sum",
        attribute_key="rosetta.cost.usd",
        group=["service.name"],
        legend="{{service.name}}",
    ),
    widget(
        title="Spend by model",
        description="Model names normalised across OpenAI, Anthropic and self-hosted.",
        operator="sum",
        attribute_key="rosetta.cost.usd",
        group=["gen_ai.request.model"],
        legend="{{gen_ai.request.model}}",
    ),
    widget(
        title="Input tokens by service",
        description=(
            "Includes billing-agent, whose counts arrived as a JSON string and were "
            "unqueryable as numbers until Rosetta unpacked them."
        ),
        operator="sum",
        attribute_key="gen_ai.usage.input_tokens",
        group=["service.name"],
        legend="{{service.name}}",
    ),
    widget(
        title="Dark cost: inference spans with no token usage",
        description=(
            "Spend invisible to every cost dashboard. Container spans are excluded "
            "-- they carry no tokens by design."
        ),
        operator="count",
        attribute_key=None,
        group=["service.name"],
        filters=bool_filter("rosetta.dark_cost"),
        legend="{{service.name}}",
    ),
    widget(
        title="Credentials redacted before storage",
        description=(
            "Prompts containing AWS keys, JWTs, private keys or connection strings, "
            "shredded in the pipeline so the secret never lands."
        ),
        operator="count",
        attribute_key=None,
        group=["service.name"],
        filters=bool_filter("rosetta.redacted"),
        legend="{{service.name}}",
    ),
    widget(
        title="Telemetry conformance score by service",
        description="100 minus weighted findings. Ranks which teams emit portable telemetry.",
        operator="avg",
        attribute_key="rosetta.conformance.score",
        group=["service.name"],
        legend="{{service.name}}",
    ),
    widget(
        title="Unpriced models",
        description=(
            "Real token spend against a model absent from the price table. Rosetta "
            "refuses to price these at zero, which would hide them."
        ),
        operator="count",
        attribute_key=None,
        group=["gen_ai.request.model"],
        filters=bool_filter("rosetta.unpriced_model"),
        legend="{{gen_ai.request.model}}",
    ),
    widget(
        title="Spans by source dialect",
        description="Which vocabularies are actually in production right now.",
        operator="count",
        attribute_key=None,
        group=["rosetta.dialect"],
        legend="{{rosetta.dialect}}",
    ),
]


def build(title: str) -> dict:
    layout = []
    for index, item in enumerate(WIDGETS):
        layout.append(
            {
                "i": item["id"],
                "x": (index % 2) * 6,
                "y": (index // 2) * 4,
                "w": 6,
                "h": 4,
            }
        )
    return {
        "title": title,
        "description": (
            "Cost, dark spend, leaked credentials and telemetry quality across four "
            "agent frameworks that share no attribute names."
        ),
        "tags": ["rosetta"],
        "widgets": WIDGETS,
        "layout": layout,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--title", default="Rosetta, polyglot agent telemetry")
    parser.add_argument("--replace", action="store_true", help="delete same-titled dashboards first")
    args = parser.parse_args(argv)

    key = _api_key()

    if args.replace:
        status, payload = call("GET", "/api/v1/dashboards", None, base=args.base, key=key)
        if status == 200 and isinstance(payload, dict):
            for item in payload.get("data") or []:
                data = item.get("data") or {}
                if data.get("title") == args.title:
                    call("DELETE", f"/api/v1/dashboards/{item['id']}", None, base=args.base, key=key)
                    print(f"[del]  existing '{args.title}' ({item['id']})")

    status, payload = call("POST", "/api/v1/dashboards", build(args.title), base=args.base, key=key)
    if status not in (200, 201):
        print(f"[FAIL] HTTP {status}: {str(payload)[:400]}")
        return 1

    dashboard_id = (payload or {}).get("data", {}).get("id")
    print(f"[ok]   dashboard '{args.title}' with {len(WIDGETS)} panels")
    print(f"       {args.base}/dashboard/{dashboard_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
