"""Query SigNoz for the numbers that were unanswerable before Rosetta.

Each query here is deliberately *one* query spanning four services that share no
attribute names. Run ``--raw`` to see the request bodies; they are ordinary
Query Builder v5, nothing bespoke.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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


def query_range(body: dict[str, Any], *, base: str, key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/v5/query_range",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "SIGNOZ-API-KEY": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise SystemExit(f"query_range HTTP {exc.code}: {detail}") from exc


def scalar_query(
    *,
    name: str,
    expression: str,
    filter_expression: str | None,
    group_by: Sequence[str],
    minutes: int,
) -> dict[str, Any]:
    """Build a scalar (table) Query Builder v5 request over trace spans."""
    now_ms = int(time.time() * 1000)
    spec: dict[str, Any] = {
        "name": "A",
        "signal": "traces",
        "stepInterval": 60,
        "aggregations": [{"expression": expression, "alias": name}],
    }
    if filter_expression:
        spec["filter"] = {"expression": filter_expression}
    if group_by:
        spec["groupBy"] = [
            {"name": g, "fieldContext": "resource" if g.startswith("service.") else "span"}
            for g in group_by
        ]
    return {
        "schemaVersion": "v1",
        "start": now_ms - minutes * 60_000,
        "end": now_ms,
        "requestType": "scalar",
        "compositeQuery": {"queries": [{"type": "builder_query", "spec": spec}]},
    }


def render(payload: dict[str, Any], *, alias: str = "value") -> list[dict[str, Any]]:
    """Flatten a scalar query_range response into plain rows.

    The v5 scalar shape keeps columns and rows apart::

        data.data.results[].columns -> [{name, columnType}, ...]
        data.data.results[].data    -> [[groupValue, aggValue], ...]

    Aggregation columns come back as ``__result_0`` regardless of the alias sent
    in the request, so they are relabelled here.
    """
    outer = payload.get("data", payload)
    results = (outer.get("data") or {}).get("results") or outer.get("results") or []

    rows: list[dict[str, Any]] = []
    for result in results:
        columns = result.get("columns") or []
        names: list[str] = []
        for index, column in enumerate(columns):
            name = column.get("name") or f"col{index}"
            if column.get("columnType") == "aggregation" or name.startswith("__result"):
                name = alias
            names.append(name)
        for values in result.get("data") or []:
            if not isinstance(values, (list, tuple)):
                continue
            rows.append({names[i]: v for i, v in enumerate(values) if i < len(names)})
    return rows


# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------

QUESTIONS: tuple[tuple[str, str, str, str | None, tuple[str, ...]], ...] = (
    (
        "total_spend",
        "Total agent spend, all four frameworks, one query",
        "sum(rosetta.cost.usd)",
        None,
        ("service.name",),
    ),
    (
        "tokens_by_service",
        "Input tokens by service -- impossible before normalisation",
        "sum(gen_ai.usage.input_tokens)",
        None,
        ("service.name",),
    ),
    (
        "spend_by_model",
        "Spend by model, across vendors",
        "sum(rosetta.cost.usd)",
        None,
        ("gen_ai.request.model",),
    ),
    (
        "dark_cost",
        "Dark cost: inference spans reporting no tokens at all",
        "count()",
        "rosetta.dark_cost = true",
        ("service.name",),
    ),
    (
        "unpriced",
        "Spend against models absent from the price table",
        "count()",
        "rosetta.unpriced_model = true",
        ("gen_ai.request.model",),
    ),
    (
        "leaks",
        "Spans where credentials were redacted before storage",
        "count()",
        "rosetta.redacted = true",
        ("service.name",),
    ),
    (
        "conformance",
        "Telemetry conformance score by service",
        "avg(rosetta.conformance.score)",
        None,
        ("service.name",),
    ),
    (
        "loops",
        "Repeated tool calls -- runaway-loop signature",
        "count()",
        "gen_ai.tool.name = 'lookup_order'",
        ("service.name",),
    ),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--raw", action="store_true", help="print request bodies")
    parser.add_argument("--only", help="run a single question by key")
    args = parser.parse_args(argv)

    key = _api_key()

    for question_key, title, expression, filter_expression, group_by in QUESTIONS:
        if args.only and args.only != question_key:
            continue
        body = scalar_query(
            name=question_key,
            expression=expression,
            filter_expression=filter_expression,
            group_by=group_by,
            minutes=args.minutes,
        )
        print(f"\n=== {title}")
        print(f"    {expression}" + (f"  WHERE {filter_expression}" if filter_expression else ""))
        if args.raw:
            print(json.dumps(body, indent=2))
        payload = query_range(body, base=args.base, key=key)
        rows = render(payload, alias=question_key)
        if not rows:
            print("    (no rows)")
            continue
        for row in rows:
            parts = [f"{k}={v}" for k, v in row.items()]
            print("    " + "  ".join(parts))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
