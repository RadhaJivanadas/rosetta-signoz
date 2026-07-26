"""USD cost computation -- the attribute no convention defines.

Verified across the four major emitters: Langfuse, LangSmith, the OpenAI Agents
SDK and LangChain all price *server-side* and export spans with no cost on them.
OpenInference has ``llm.cost.*`` fields but they are normally filled in by
Phoenix after ingest, so a direct OTLP export carries tokens and no money.

The consequence is that a self-hosted OTLP backend can show you a token count
but not a bill. Rosetta computes the dollars in the pipeline, where the model
name and token counts are both already present.

Three rules keep the output honest:

1. **Never invent a price.** An unknown model yields ``None``, not zero, and is
   flagged. Silently pricing an unknown model at $0 turns real spend invisible,
   which is the exact failure this project exists to expose.
2. **Never overwrite a real one.** A cost that arrived on the span from a vendor
   is kept, and ``rosetta.cost.source`` records which.
3. **Never double-count.** Cache reads and writes are billed at their own rates
   and are *not* re-billed as ordinary input tokens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .semconv import Canon, Rosetta

DEFAULT_PRICING_PATH = Path(__file__).with_name("pricing.yaml")

#: Rates in the table are quoted per this many tokens.
_RATE_UNIT = 1_000_000


class PricingCode:
    """Finding codes emitted by the pricing pass (continues normalize.Code)."""

    UNPRICED_MODEL = "R012"   # tokens known, model absent from the price table
    SELF_HOSTED = "R013"      # priced at zero by policy, not by ignorance


@dataclass(frozen=True)
class ModelRate:
    """Per-token rates for one model, in USD per million tokens."""

    name: str
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    #: "output" when reasoning tokens bill at the output rate (o-series).
    reasoning_as: str = "output"
    self_hosted: bool = False


@dataclass
class CostBreakdown:
    """Result of pricing one span."""

    input_usd: float = 0.0
    output_usd: float = 0.0
    total_usd: float = 0.0
    model: str | None = None
    matched_rate: str | None = None
    #: "computed" | "passthrough:<dialect>" | None when not priceable.
    source: str | None = None
    #: True only when tokens were present but the model has no rate. Distinct
    #: from "no tokens at all", which is dark cost and a different diagnosis --
    #: conflating them made every dark-cost span also report an unpriced model.
    unpriced_model: bool = False
    findings: list[tuple[str, str, str]] = None  # (code, severity, message)

    def __post_init__(self) -> None:
        if self.findings is None:
            self.findings = []

    @property
    def priced(self) -> bool:
        return self.source is not None


class PricingTable:
    """Model price lookup with longest-prefix alias resolution.

    Providers append dated suffixes to model identifiers -- ``gpt-4o-2024-11-20``,
    ``claude-sonnet-4-20250514``, ``models/gemini-2.5-pro``. A table keyed on
    exact strings stops matching silently the first time a model rolls forward,
    so every lookup falls back to longest-prefix matching.
    """

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self.version: str = str(spec.get("version", "unknown"))
        self.currency: str = str(spec.get("currency", "USD"))
        self._rates: dict[str, ModelRate] = {}
        for name, row in (spec.get("models") or {}).items():
            self._rates[name] = ModelRate(
                name=name,
                input=float(row.get("input", 0.0)),
                output=float(row.get("output", 0.0)),
                cache_read=(
                    float(row["cache_read"]) if row.get("cache_read") is not None else None
                ),
                cache_write=(
                    float(row["cache_write"]) if row.get("cache_write") is not None else None
                ),
                reasoning_as=str(row.get("reasoning_as", "output")),
                self_hosted=bool(row.get("self_hosted", False)),
            )
        # Longest alias first so "gpt-4o-mini" is tested before "gpt-4o".
        aliases: Mapping[str, str] = spec.get("aliases") or {}
        self._aliases: list[tuple[str, str]] = sorted(
            aliases.items(), key=lambda kv: len(kv[0]), reverse=True
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PricingTable":
        """Load a pricing table. ``ROSETTA_PRICING_FILE`` overrides the default."""
        chosen = Path(path or os.environ.get("ROSETTA_PRICING_FILE") or DEFAULT_PRICING_PATH)
        with chosen.open("r", encoding="utf-8") as handle:
            return cls(yaml.safe_load(handle) or {})

    def resolve(self, model: str | None) -> ModelRate | None:
        """Find the rate for a model identifier, tolerating vendor suffixes."""
        if not model:
            return None
        key = model.strip().lower()
        # Strip a provider namespace: "models/gemini-2.5-pro", "openai/gpt-4o".
        if "/" in key:
            key = key.rsplit("/", 1)[-1]
        if key in self._rates:
            return self._rates[key]
        for prefix, target in self._aliases:
            if key.startswith(prefix):
                rate = self._rates.get(target)
                if rate is not None:
                    return rate
        return None

    def price(
        self,
        attrs: Mapping[str, Any],
        *,
        is_aggregate: bool = False,
    ) -> CostBreakdown:
        """Compute the USD cost of one normalised span.

        ``attrs`` must already carry canonical ``gen_ai.*`` keys -- run
        :func:`rosetta.normalize.normalize` first.
        """
        result = CostBreakdown()

        # Rule 2: a real vendor cost always wins.
        existing = attrs.get(Rosetta.COST_USD.value)
        if isinstance(existing, (int, float)) and existing > 0:
            result.total_usd = float(existing)
            result.input_usd = float(attrs.get(Rosetta.COST_INPUT_USD.value) or 0.0)
            result.output_usd = float(attrs.get(Rosetta.COST_OUTPUT_USD.value) or 0.0)
            result.source = str(attrs.get(Rosetta.COST_SOURCE.value) or "passthrough:unknown")
            result.model = attrs.get(Canon.REQUEST_MODEL.value)
            return result

        model = attrs.get(Canon.REQUEST_MODEL.value) or attrs.get(Canon.RESPONSE_MODEL.value)
        result.model = model

        def count(key: Canon) -> int:
            raw = attrs.get(key.value)
            return raw if isinstance(raw, int) and raw > 0 else 0

        n_input = count(Canon.INPUT_TOKENS)
        n_output = count(Canon.OUTPUT_TOKENS)
        n_reasoning = count(Canon.REASONING_TOKENS)
        n_cache_read = count(Canon.CACHE_READ_TOKENS)
        n_cache_write = count(Canon.CACHE_WRITE_TOKENS)

        if not any((n_input, n_output, n_reasoning, n_cache_read, n_cache_write)):
            return result  # nothing to price; dark cost is normalize's finding

        rate = self.resolve(model)
        if rate is None:
            # Rule 1: refuse to guess.
            result.unpriced_model = True
            result.findings.append(
                (
                    PricingCode.UNPRICED_MODEL,
                    "warning",
                    f"model {model!r} is not in pricing table {self.version!r}; "
                    "tokens are recorded but spend is unknown",
                )
            )
            return result

        if rate.self_hosted:
            result.findings.append(
                (
                    PricingCode.SELF_HOSTED,
                    "info",
                    f"{rate.name} is self-hosted: per-token price is zero by policy, "
                    "real cost is GPU time and is not captured here",
                )
            )

        # Rule 3: cached tokens bill at their own rate and are not re-billed as
        # ordinary input. Emitters differ on whether cache counts are included
        # in input_tokens; we subtract to avoid charging twice, floored at zero.
        billable_input = max(0, n_input - n_cache_read - n_cache_write)

        input_usd = billable_input * rate.input / _RATE_UNIT
        if n_cache_read:
            read_rate = rate.cache_read if rate.cache_read is not None else rate.input
            input_usd += n_cache_read * read_rate / _RATE_UNIT
        if n_cache_write:
            write_rate = rate.cache_write if rate.cache_write is not None else rate.input
            input_usd += n_cache_write * write_rate / _RATE_UNIT

        output_rate = rate.output if rate.reasoning_as == "output" else rate.input
        output_usd = n_output * rate.output / _RATE_UNIT
        output_usd += n_reasoning * output_rate / _RATE_UNIT

        result.input_usd = round(input_usd, 8)
        result.output_usd = round(output_usd, 8)
        result.total_usd = round(input_usd + output_usd, 8)
        result.matched_rate = rate.name
        result.source = "computed"
        return result

    def apply(
        self,
        attrs: dict[str, Any],
        *,
        is_aggregate: bool = False,
    ) -> CostBreakdown:
        """Price a span and write the cost attributes back into ``attrs``."""
        breakdown = self.price(attrs, is_aggregate=is_aggregate)
        if breakdown.source == "computed":
            attrs[Rosetta.COST_USD.value] = breakdown.total_usd
            attrs[Rosetta.COST_INPUT_USD.value] = breakdown.input_usd
            attrs[Rosetta.COST_OUTPUT_USD.value] = breakdown.output_usd
            attrs[Rosetta.COST_SOURCE.value] = "computed"
            attrs[Rosetta.PRICING_VERSION.value] = self.version
        elif breakdown.unpriced_model:
            attrs[Rosetta.UNPRICED_MODEL.value] = True
        return breakdown
