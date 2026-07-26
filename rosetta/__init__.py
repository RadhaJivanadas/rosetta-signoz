"""Rosetta -- one canonical vocabulary for polyglot GenAI telemetry.

AI agent frameworks emit mutually incompatible OpenTelemetry attributes. An
OTLP-native backend such as SigNoz stores exactly what it receives, so a fleet
running more than one framework cannot answer "what did all my agents cost" with
a single query. Rosetta normalises every known dialect into one vocabulary,
computes the USD cost that no convention defines, redacts secrets before they
reach storage, and scores each service on how conformant its telemetry is.
"""

from .normalize import Code, Finding, Normalized, apply, normalize
from .semconv import DIALECTS, Canon, Dialect, Rosetta

__all__ = [
    "Canon",
    "Code",
    "DIALECTS",
    "Dialect",
    "Finding",
    "Normalized",
    "Rosetta",
    "apply",
    "normalize",
]

__version__ = "0.1.0"
