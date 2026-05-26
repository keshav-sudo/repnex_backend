from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.exceptions import ValidationFailed
from app.query_engine.template_loader import SQLTemplate


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
    t = spec["type"]
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
    if t == "str":
        return str(value)
    if t == "enum":
        if value not in spec["values"]:
            raise ValidationFailed(f"param {name}: must be one of {spec['values']}")
        return value
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
    raise ValidationFailed(f"param {name}: unknown type {t}")


def _derive_period(params: dict[str, Any]) -> dict[str, Any]:
    period = params.get("period")
    if period not in _PERIOD_DELTAS:
        return {}
    end = datetime.now(timezone.utc)
    start = end - _PERIOD_DELTAS[period]
    return {"start": start, "end": end}


def bind(
    template: SQLTemplate, raw_params: dict[str, Any], *, db_type: str
) -> BoundQuery:
    if template.supported_dbs and db_type not in template.supported_dbs:
        raise ValidationFailed(
            f"Template {template.id} not supported on {db_type}",
        )

    # Allowlist + coerce + defaults
    bound: dict[str, Any] = {}
    for name, spec in template.params.items():
        value = raw_params.get(name, spec.get("default"))
        if value is None:
            raise ValidationFailed(f"Missing required param: {name}")
        bound[name] = _coerce(name, spec, value)

    extra = set(raw_params) - set(template.params)
    if extra:
        raise ValidationFailed(f"Unknown params: {sorted(extra)}")

    # Derived
    if "start" in template.derived_params or "end" in template.derived_params:
        bound.update(_derive_period(bound))

    sql = template.sql_for(db_type)
    return BoundQuery(sql=sql, params=bound, db_type=db_type)
