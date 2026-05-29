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


def _coerce(name: str, spec: dict[str, Any], value: Any) -> Any:
    t = spec.get("type", "str")
    if t == "int":
        try:
            v = int(value)
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
        return str(value)
    if t == "enum":
        if value not in spec.get("values", []):
            raise ValidationFailed(f"param {name}: must be one of {spec.get('values', [])}")
        return value
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "y")
        return bool(value)
    if t == "date":
        if isinstance(value, datetime):
            return value.date()
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError as e:
            raise ValidationFailed(f"param {name}: invalid date") from e
    if t == "datetime":
        try:
            return datetime.fromisoformat(str(value))
        except ValueError as e:
            raise ValidationFailed(f"param {name}: invalid datetime") from e
    # Unknown type — pass through as string
    return str(value)


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
