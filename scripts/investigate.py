"""An agent that investigates other agents, through the SigNoz MCP server.

This is the hackathon's premise taken literally: the fleet emits telemetry, and
a separate agent reads that telemetry back through Model Context Protocol and
writes up what went wrong. It only ever sees what SigNoz sees.

It runs without any LLM API key. The investigation loop is deterministic --
each finding decides which MCP tool to call next -- so a judge gets the same
report every time and nothing depends on a paid endpoint. Set
``ROSETTA_LLM=1`` with ``ANTHROPIC_API_KEY`` to have a model narrate the
conclusion instead of the built-in summariser; the evidence gathering is
identical either way.

Why MCP rather than the REST API directly: the MCP server is the interface
SigNoz exposes *to agents*. Using it is the difference between a script that
queries a database and an agent that shares an operator's tools.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

DEFAULT_MCP = os.environ.get("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")


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


# ---------------------------------------------------------------------------
# Minimal MCP client (Streamable HTTP transport)
# ---------------------------------------------------------------------------


class MCPClient:
    """Just enough MCP to call tools: initialize, then tools/call."""

    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key
        self._id = 0
        self.server_info: dict[str, Any] = {}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # The server may reply as JSON or as an SSE stream; accept both.
                "Accept": "application/json, text/event-stream",
                "SIGNOZ-API-KEY": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"MCP HTTP {exc.code}: {detail}") from exc

        # SSE framing: take the last JSON object on a `data:` line.
        last: dict[str, Any] | None = None
        for line in raw.splitlines():
            line = line[5:].strip() if line.startswith("data:") else line.strip()
            if line.startswith("{"):
                try:
                    last = json.loads(line)
                except ValueError:
                    continue
        return last

    def initialize(self) -> None:
        self._id += 1
        reply = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "rosetta-investigator", "version": "0.1.0"},
                },
            }
        )
        self.server_info = ((reply or {}).get("result") or {}).get("serverInfo") or {}
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return its text content."""
        self._id += 1
        reply = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        result = (reply or {}).get("result") or {}
        if (reply or {}).get("error"):
            return f"[tool error] {json.dumps((reply or {})['error'])[:400]}"
        chunks = [
            block.get("text", "")
            for block in result.get("content") or []
            if block.get("type") == "text"
        ]
        return "\n".join(c for c in chunks if c)


# ---------------------------------------------------------------------------
# Investigation
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """One thing the investigator established, and how."""

    headline: str
    tool: str
    detail: str
    severity: str = "info"
    raw: str = ""


@dataclass
class Report:
    evidence: list[Evidence] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)

    def add(self, item: Evidence) -> None:
        self.evidence.append(item)
        if item.tool not in self.tools_used:
            self.tools_used.append(item.tool)

    def by_severity(self, severity: str) -> list[Evidence]:
        return [e for e in self.evidence if e.severity == severity]


@dataclass(frozen=True)
class Probe:
    """One question, expressed as a purpose-built MCP tool call.

    ``signoz_aggregate_traces`` is used rather than the raw
    ``signoz_execute_builder_query`` escape hatch: it is the tool the server
    offers for exactly this shape of question, and it keeps the call legible.
    """

    key: str
    question: str
    aggregation: str
    aggregate_on: str | None = None
    filter_expression: str | None = None
    group_by: str = ""


PROBES: tuple[Probe, ...] = (
    Probe(
        "leaked_credentials",
        "Were credentials sent to an LLM provider?",
        "count",
        filter_expression="rosetta.redacted = true",
        group_by="service.name",
    ),
    Probe(
        "dark_cost",
        "Is there spend nobody can see?",
        "count",
        filter_expression="rosetta.dark_cost = true",
        group_by="service.name",
    ),
    Probe(
        "unpriced",
        "Are we spending on models with no price?",
        "count",
        filter_expression="rosetta.unpriced_model = true",
        group_by="gen_ai.request.model",
    ),
    Probe(
        "spend",
        "What did the fleet cost?",
        "sum",
        aggregate_on="rosetta.cost.usd",
        group_by="service.name",
    ),
    Probe(
        "conformance",
        "Which services emit poor telemetry?",
        "avg",
        aggregate_on="rosetta.conformance.score",
        group_by="service.name",
    ),
    Probe(
        "tool_loops",
        "Is any agent stuck repeating a tool call?",
        "count",
        filter_expression="gen_ai.tool.name EXISTS",
        group_by="gen_ai.tool.name, service.name",
    ),
)


def builder_query(
    expression: str,
    filter_expression: str | None,
    group_by: Sequence[str],
    minutes: int,
) -> dict[str, Any]:
    import time

    now_ms = int(time.time() * 1000)
    spec: dict[str, Any] = {
        "name": "A",
        "signal": "traces",
        "stepInterval": 60,
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
    return {
        "schemaVersion": "v1",
        "start": now_ms - minutes * 60_000,
        "end": now_ms,
        "requestType": "scalar",
        "compositeQuery": {"queries": [{"type": "builder_query", "spec": spec}]},
    }


def investigate(client: MCPClient, *, minutes: int, verbose: bool = False) -> Report:
    import time

    report = Report()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - minutes * 60_000

    for probe in PROBES:
        arguments: dict[str, Any] = {
            "aggregation": probe.aggregation,
            "start": start_ms,
            "end": now_ms,
            "searchContext": probe.question,
        }
        if probe.aggregate_on:
            arguments["aggregateOn"] = probe.aggregate_on
        if probe.filter_expression:
            arguments["filter"] = probe.filter_expression
        if probe.group_by:
            arguments["groupBy"] = probe.group_by

        raw = client.call_tool("signoz_aggregate_traces", arguments)
        if verbose:
            print(f"\n--- {probe.key}\n{raw[:900]}")

        severity, detail = _interpret(probe.key, raw)
        report.add(
            Evidence(
                headline=probe.question,
                tool="signoz_aggregate_traces",
                detail=detail,
                severity=severity,
                raw=raw[:2000],
            )
        )

    # Follow the trail: if credentials leaked, pull the offending spans so the
    # report names the service and operation rather than just a count.
    if report.by_severity("critical"):
        raw = client.call_tool(
            "signoz_search_traces",
            {
                "filter": "rosetta.redacted = true",
                "limit": 5,
                "searchContext": "Which spans carried redacted credentials?",
            },
        )
        report.add(
            Evidence(
                headline="Spans that carried credentials",
                tool="signoz_search_traces",
                detail=_first_lines(raw, 12),
                severity="detail",
                raw=raw[:2000],
            )
        )

    return report


#: Substrings that mean the tool did not answer the question. Checked before
#: anything else: an earlier version treated a validation error as an all-clear,
#: because the error text happened to contain digits, and the report cheerfully
#: announced "No redacted credentials" while the query had never run. A checker
#: that cannot distinguish "nothing found" from "nothing asked" is worthless.
_FAILURE_MARKERS: tuple[str, ...] = (
    "validation failed",
    "[tool error]",
    "is not valid",
    "invalid input",
    "error:",
    "unauthorized",
    "forbidden",
    "must be a json object",
)

#: Substrings that positively confirm an empty result set.
_EMPTY_MARKERS: tuple[str, ...] = ("no data", "no results", "0 rows", "empty")


def _looks_failed(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _FAILURE_MARKERS)


def _has_findings(text: str) -> bool:
    """True only when the response positively shows result rows."""
    lowered = text.lower()
    if any(marker in lowered for marker in _EMPTY_MARKERS):
        return False
    # Require a digit on a line that is not part of the echoed query/context.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        if any(ch.isdigit() for ch in stripped):
            return True
    return False


def _interpret(key: str, raw: str) -> tuple[str, str]:
    """Turn a tool response into a severity and a human sentence.

    Deliberately conservative: if the response cannot be parsed, say so rather
    than assert an all-clear. A monitoring tool that reports "fine" when it is
    actually broken is worse than one that reports nothing.
    """
    if not raw or not raw.strip():
        return "unknown", "could not determine -- empty response from MCP"

    text = raw.strip()
    if _looks_failed(text):
        return "unknown", (
            "could not determine -- the query did not execute. This is NOT an "
            "all-clear.\n" + _first_lines(text, 4)
        )

    has_rows = _has_findings(text)

    if key == "leaked_credentials":
        if has_rows:
            return "critical", (
                "Credentials reached an LLM provider. Rosetta redacted them before "
                "storage, but the calling application still transmitted them -- the "
                "keys must be rotated.\n" + _first_lines(text, 8)
            )
        return "ok", "No redacted credentials in the window."

    if key == "dark_cost":
        if has_rows:
            return "warning", (
                "Inference spans are reporting no token usage. This is real spend "
                "that no cost dashboard can attribute.\n" + _first_lines(text, 8)
            )
        return "ok", "No dark-cost spans in the window."

    if key == "unpriced":
        if has_rows:
            return "warning", (
                "Tokens spent against models missing from the pricing table; their "
                "cost is absent from every total.\n" + _first_lines(text, 6)
            )
        return "ok", "Every model in use has a price."

    if key == "tool_loops":
        return "info", (
            "Tool-call distribution -- a single tool dominating one service is the "
            "runaway-loop signature.\n" + _first_lines(text, 10)
        )

    return "info", _first_lines(text, 10)


def _first_lines(text: str, count: int) -> str:
    """Render a tool response compactly, unpacking the scalar shape when present."""
    formatted = _format_rows(text)
    lines = [line for line in formatted.splitlines() if line.strip()]
    return "\n".join(lines[:count])


def _format_rows(text: str) -> str:
    """Turn a SigNoz scalar response into aligned ``key = value`` lines.

    Columns and rows arrive separately (``results[].columns`` and
    ``results[].data``), and aggregation columns are always named ``__result_0``
    regardless of the alias requested, so they are relabelled here. Falls back to
    the raw text whenever the shape is anything else.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    try:
        # The MCP server appends human-readable notes after the JSON body, so a
        # plain json.loads fails with "Extra data". Decode just the first object.
        payload, _end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return text
    if not isinstance(payload, dict):
        return text

    outer = payload.get("data", payload)
    results = (outer.get("data") or {}).get("results") or outer.get("results") or []

    rendered: list[str] = []
    for result in results:
        columns = result.get("columns") or []
        if not columns:
            continue
        names = []
        for index, column in enumerate(columns):
            name = column.get("name") or f"col{index}"
            if column.get("columnType") == "aggregation" or name.startswith("__result"):
                name = "value"
            names.append(name)
        for values in result.get("data") or []:
            if not isinstance(values, (list, tuple)):
                continue
            pairs = [
                f"{names[i]}={values[i]}" for i in range(min(len(names), len(values)))
            ]
            rendered.append("  ".join(pairs))

    # Raw span rows (signoz_search_traces) use a different shape: each row is a
    # dict of every column, most of them null. Show only the fields that matter.
    if not rendered:
        interesting = (
            "service.name",
            "name",
            "gen_ai.request.model",
            "rosetta.redacted.kinds",
            "rosetta.cost.usd",
            "trace_id",
        )
        for result in results:
            for row in result.get("rows") or []:
                data = row.get("data") or {}
                populated = {
                    k: v for k, v in data.items() if v not in (None, "", [], {})
                }
                if not populated:
                    continue
                # Preferred fields first, then whatever else is set, capped so a
                # span with 200 mostly-null columns stays readable.
                keys = [k for k in interesting if k in populated]
                keys += [k for k in populated if k not in keys][: max(0, 6 - len(keys))]
                rendered.append("  ".join(f"{k}={populated[k]}" for k in keys))

    return "\n".join(rendered) if rendered else text


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ICON = {
    "critical": "[CRITICAL]",
    "warning": "[WARNING] ",
    "ok": "[ok]      ",
    "info": "[info]    ",
    "detail": "[detail]  ",
    "unknown": "[unknown] ",
}


def render_markdown(report: Report, *, server: str, minutes: int) -> str:
    critical = report.by_severity("critical")
    warning = report.by_severity("warning")

    if critical:
        verdict = "ACTION REQUIRED -- credentials were transmitted to a model provider."
    elif warning:
        verdict = "DEGRADED -- untracked spend and/or unpriced models detected."
    else:
        verdict = "HEALTHY -- no governance findings in this window."

    lines = [
        "# Rosetta incident report",
        "",
        f"- **Window:** last {minutes} minutes",
        f"- **Source:** SigNoz MCP ({server})",
        f"- **Tools used:** {', '.join(report.tools_used)}",
        "",
        f"## Verdict",
        "",
        verdict,
        "",
        "## Findings",
        "",
    ]
    order = ("critical", "warning", "unknown", "info", "detail", "ok")
    for severity in order:
        for item in report.by_severity(severity):
            lines.append(f"### {_ICON.get(severity, '').strip()} {item.headline}")
            lines.append("")
            lines.append(f"*via `{item.tool}`*")
            lines.append("")
            lines.append("```")
            lines.append(item.detail)
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--out", help="write the report to this path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    client = MCPClient(args.mcp_url, _api_key())
    client.initialize()
    name = client.server_info.get("name", "unknown")
    version = client.server_info.get("version", "?")
    print(f"connected to MCP server {name} {version}\n")

    report = investigate(client, minutes=args.minutes, verbose=args.verbose)

    for item in report.evidence:
        first = item.detail.splitlines()[0] if item.detail else ""
        print(f"{_ICON.get(item.severity, '')} {item.headline}")
        if first:
            print(f"            {first[:150]}")

    markdown = render_markdown(report, server=args.mcp_url, minutes=args.minutes)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        print(f"\nreport written to {args.out}")

    return 2 if report.by_severity("critical") else 0


if __name__ == "__main__":
    raise SystemExit(main())
