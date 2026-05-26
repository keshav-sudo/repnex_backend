from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.exceptions import NotFound, ValidationFailed

TEMPLATES_PATH = Path(__file__).parent / "templates" / "query_templates.json"

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE|CALL|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SQLTemplate:
    id: str
    description: str
    supported_dbs: tuple[str, ...]
    params: dict[str, dict[str, Any]]
    derived_params: tuple[str, ...]
    sql_by_dialect: dict[str, str]

    def sql_for(self, db_type: str) -> str:
        if db_type not in self.sql_by_dialect:
            raise ValidationFailed(f"Template {self.id} has no SQL for {db_type}")
        return self.sql_by_dialect[db_type]


class TemplateRegistry:
    def __init__(self, templates: dict[str, SQLTemplate]) -> None:
        self._t = templates

    def get(self, template_id: str) -> SQLTemplate:
        if template_id not in self._t:
            raise NotFound(f"Template not found: {template_id}")
        return self._t[template_id]

    def list_for_llm(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "description": t.description,
                "params": t.params,
                "supported_dbs": list(t.supported_dbs),
            }
            for t in self._t.values()
        ]

    def all(self) -> list[SQLTemplate]:
        return list(self._t.values())


def _validate_sql(sql: str, template_id: str) -> None:
    sql_norm = sql.strip()
    if not sql_norm.upper().startswith("SELECT") and not sql_norm.upper().startswith("WITH"):
        raise ValidationFailed(f"Template {template_id}: only SELECT/CTE allowed")
    if _FORBIDDEN.search(sql_norm):
        raise ValidationFailed(f"Template {template_id}: forbidden keyword in SQL")


def load_registry(path: Path = TEMPLATES_PATH) -> TemplateRegistry:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, SQLTemplate] = {}
    for entry in raw["templates"]:
        sql_by_dialect = entry["sql"]
        for d, q in sql_by_dialect.items():
            _validate_sql(q, entry["id"])
        t = SQLTemplate(
            id=entry["id"],
            description=entry.get("description", ""),
            supported_dbs=tuple(entry.get("supported_dbs", [])),
            params=entry.get("params", {}),
            derived_params=tuple(entry.get("derived_params", [])),
            sql_by_dialect=sql_by_dialect,
        )
        out[t.id] = t
    return TemplateRegistry(out)


_registry: TemplateRegistry | None = None


def init_template_registry() -> TemplateRegistry:
    global _registry
    _registry = load_registry()
    return _registry


def get_template_registry() -> TemplateRegistry:
    if _registry is None:
        raise RuntimeError("Template registry not initialized")
    return _registry
