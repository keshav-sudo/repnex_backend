"""Parameter Binder — lightweight SQL parameter types for the semantic engine.

In the V2 semantic pipeline, SQL is generated directly by the LLM from the
YAML knowledge graph.  Parameters (dates, limits) are extracted from the NL query
and injected into the prompt — NOT bound into the SQL via psycopg placeholders.

This module provides:
  - ``BoundQuery``   — immutable value object carrying the generated SQL + dialect.
  - ``sanitize_string`` — shared input-sanitization helper used by services.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_MAX_STRING_PARAM_LEN = 500

_DANGEROUS_PATTERNS = re.compile(
    r"<script|javascript:|on\w+=|&#|%3C|%3E|UNION\s+SELECT|INTO\s+OUTFILE|xp_cmdshell",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BoundQuery:
    """Immutable value object produced by the semantic engine.

    Attributes:
        sql:     The generated SQL string (no placeholders — fully rendered).
        params:  Supplementary metadata (e.g. ``{"start_date": "...", "end_date": "..."}``)
                 stored for audit purposes only; NOT used for DB binding.
        db_type: Target database dialect (``"mssql"``, ``"postgres"``, ``"mysql"``).
    """

    sql: str
    params: dict[str, Any]
    db_type: str


def sanitize_string(value: str) -> str:
    """Strip dangerous patterns and enforce max-length on any string input."""
    cleaned = value.strip()
    if len(cleaned) > _MAX_STRING_PARAM_LEN:
        cleaned = cleaned[:_MAX_STRING_PARAM_LEN]
    return _DANGEROUS_PATTERNS.sub("", cleaned)
