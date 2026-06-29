from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.exceptions import ValidationFailed
from app.query_engine.template_loader import SQLTemplate
from app.schemas.query import MissingParam


@dataclass(frozen=True, slots=True)
class BoundQuery:
    sql: str
    params: dict[str, Any]
    db_type: str


_PERIOD_DELTAS = {
    "last_week": timedelta(days=7),
    "last_month": timedelta(days=30),
    "last_quarter": timedelta(days=90),
}


import re as _re

# Max length for string parameters (prevents LLM cost explosion / DoS)
_MAX_STRING_PARAM_LEN = 500

# Patterns to strip from string parameters (XSS / injection defense)
_DANGEROUS_PATTERNS = _re.compile(
    r"<script|javascript:|on\w+=|&#|%3C|%3E|UNION\s+SELECT|INTO\s+OUTFILE|xp_cmdshell",
    _re.IGNORECASE,
)


def _sanitize_string(value: str) -> str:
    """Strip dangerous patterns and enforce length limits on string params."""
    cleaned = value.strip()
    if len(cleaned) > _MAX_STRING_PARAM_LEN:
        cleaned = cleaned[:_MAX_STRING_PARAM_LEN]
    # Remove dangerous patterns
    cleaned = _DANGEROUS_PATTERNS.sub("", cleaned)
    return cleaned


def _coerce(name: str, spec: dict[str, Any], value: Any) -> Any:
    t = spec.get("type", "str")

    # Handle null / None / empty from LLM
    if value is None or (isinstance(value, str) and value.strip().lower() in ("null", "none", "")):
        default = spec.get("default")
        if default is not None:
            return default
        if not spec.get("required", True):
            return None
        raise ValidationFailed(f"param {name}: received null/empty but is required")

    # Handle arrays — LLM sometimes wraps values in a list
    if isinstance(value, (list, tuple)):
        if len(value) == 1:
            value = value[0]
        elif len(value) == 0:
            default = spec.get("default")
            if default is not None:
                return default
            raise ValidationFailed(f"param {name}: empty array")
        else:
            # For string types, join; for others, take first
            if t in ("str", "string"):
                value = ", ".join(str(v) for v in value)
            else:
                value = value[0]

    if t == "int":
        try:
            v = int(float(value))  # float() first to handle "10.0"
        except (TypeError, ValueError) as e:
            raise ValidationFailed(f"param {name}: must be int") from e
        if "min" in spec and v < spec["min"]:
            raise ValidationFailed(f"param {name}: min {spec['min']}")
        if "max" in spec and v > spec["max"]:
            raise ValidationFailed(f"param {name}: max {spec['max']}")
        return v
    if t == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise ValidationFailed(f"param {name}: must be float") from e
    if t in ("str", "string"):
        return _sanitize_string(str(value))
    if t == "enum":
        str_val = str(value).strip()
        allowed = spec.get("values", [])
        if str_val not in allowed:
            # Case-insensitive fallback
            lower_map = {v.lower(): v for v in allowed}
            if str_val.lower() in lower_map:
                return lower_map[str_val.lower()]
            raise ValidationFailed(f"param {name}: must be one of {allowed}")
        return str_val
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "y")
        return bool(value)
    if t == "date":
        if isinstance(value, datetime):
            return value.date()
        str_val = str(value).strip()
        # Handle common date formats
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(str_val, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(str_val).date()
        except ValueError as e:
            raise ValidationFailed(f"param {name}: invalid date '{str_val}'") from e
    if t == "datetime":
        try:
            return datetime.fromisoformat(str(value))
        except ValueError as e:
            raise ValidationFailed(f"param {name}: invalid datetime") from e
    # Unknown type — pass through as sanitized string
    return _sanitize_string(str(value))


def _derive_period(params: dict[str, Any]) -> dict[str, Any]:
    period = params.get("period")
    if period not in _PERIOD_DELTAS:
        return {}
    end = datetime.now(timezone.utc)
    start = end - _PERIOD_DELTAS[period]
    return {"start": start, "end": end}


def _resolve_natural_dates(raw_params: dict[str, Any]) -> dict[str, Any]:
    """Convert natural language date phrases to ISO dates before coercion."""
    from datetime import date, timedelta
    today = date.today()
    phrases = {
        "last 6 months": (today - timedelta(days=180), today),
        "last six months": (today - timedelta(days=180), today),
        "last 3 months": (today - timedelta(days=90), today),
        "last month": (today - timedelta(days=30), today),
        "last quarter": (today - timedelta(days=90), today),
        "last year": (today - timedelta(days=365), today),
        "this year": (date(today.year, 1, 1), today),
        "ytd": (date(today.year, 1, 1), today),
    }
    result = dict(raw_params)
    # If start_date is a phrase, resolve both start and end
    sd = str(raw_params.get("start_date", "")).lower().strip()
    if sd in phrases:
        start, end = phrases[sd]
        result["start_date"] = start.isoformat()
        result["end_date"] = raw_params.get("end_date") or end.isoformat()
    return result


def find_missing_params(
    template: SQLTemplate, raw_params: dict[str, Any]
) -> list[MissingParam]:
    """Return list of required params not provided and without defaults."""
    raw_params = _resolve_natural_dates(raw_params)
    missing: list[MissingParam] = []
    for name, spec in template.params.items():
        is_required = spec.get("required", True)
        has_default = "default" in spec
        value = raw_params.get(name)

        if value is None and is_required and not has_default:
            missing.append(
                MissingParam(
                    name=name,
                    type=spec.get("type", "string"),
                    description=spec.get("description"),
                    options=spec.get("values"),  # For enum types
                    default=spec.get("default"),
                    required=is_required,
                    min_val=spec.get("min"),
                    max_val=spec.get("max"),
                )
            )
    return missing



def bind(
    template: SQLTemplate, raw_params: dict[str, Any], *, db_type: str
) -> BoundQuery:
    supported = list(template.supported_dbs) if template.supported_dbs else []
    if "mssql" in supported:
        supported.extend(["postgres", "cloudsql"])
    if supported and db_type not in supported:
        raise ValidationFailed(
            f"Template {template.id} not supported on {db_type}",
        )

    raw_params = _resolve_natural_dates(raw_params)

    # Allowlist + coerce + defaults
    bound: dict[str, Any] = {}
    for name, spec in template.params.items():
        value = raw_params.get(name, spec.get("default"))
        if value is None:
            is_required = spec.get("required", True)
            if is_required:
                raise ValidationFailed(f"Missing required param: {name}")
            continue  # Skip optional params without value
        bound[name] = _coerce(name, spec, value)


    # Don't fail on extra params — just ignore them
    # (Pinecone-sourced templates might not have exhaustive param lists)

    # Derived
    if "start" in template.derived_params or "end" in template.derived_params:
        bound.update(_derive_period(bound))

    sql = template.sql_for(db_type)
    return BoundQuery(sql=sql, params=bound, db_type=db_type)
