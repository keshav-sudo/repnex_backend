from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.exceptions import NotFound, ValidationFailed

TEMPLATES_PATH = Path(__file__).parent / "templates" / "query_templates.json"
COMBINED_TEMPLATES_PATH_PACKAGED = Path(__file__).parent / "templates" / "all_templates_combined.json"
COMBINED_TEMPLATES_PATH = (
    Path(__file__).parents[3] / "repnex_sql_templates" / "all_templates_combined.json"
)

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE|CALL|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SQLTemplate:
    id: str
    description: str
    module: str
    category: str
    supported_dbs: tuple[str, ...]
    params: dict[str, dict[str, Any]]
    derived_params: tuple[str, ...]
    sql_by_dialect: dict[str, str]
    result_columns: tuple[str, ...]
    keywords: tuple[str, ...]
    embedding_text: str

    def sql_for(self, db_type: str) -> str:
        if db_type in self.sql_by_dialect:
            sql = self.sql_by_dialect[db_type]
        elif "mssql" in self.sql_by_dialect:
            sql = self.sql_by_dialect["mssql"]
        elif self.sql_by_dialect:
            sql = next(iter(self.sql_by_dialect.values()))
        else:
            raise ValidationFailed(f"Template {self.id} has no SQL for {db_type}")

        # If target db is postgres/cloudsql and we fallback to MSSQL SQL, adapt it
        if db_type in ("postgres", "cloudsql") and ("postgres" not in self.sql_by_dialect):
            import re

            # 1. Translate SELECT TOP %(limit)s to SELECT ... LIMIT %(limit)s
            top_match = re.search(r"\bSELECT\s+TOP\s+%\((\w+)\)s\b", sql, re.IGNORECASE)
            if top_match:
                limit_var = top_match.group(1)
                sql = re.sub(r"\bSELECT\s+TOP\s+%\(\w+\)s\b", "SELECT", sql, flags=re.IGNORECASE)
                if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
                    sql = f"{sql} LIMIT %({limit_var})s"
            else:
                top_num_match = re.search(r"\bSELECT\s+TOP\s+(\d+)\b", sql, re.IGNORECASE)
                if top_num_match:
                    limit_val = top_num_match.group(1)
                    sql = re.sub(r"\bSELECT\s+TOP\s+\d+\b", "SELECT", sql, flags=re.IGNORECASE)
                    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
                        sql = f"{sql} LIMIT {limit_val}"

            # 2. Translate GETDATE() -> CURRENT_DATE
            sql = re.sub(r"\bGETDATE\s*\(\s*\)", "CURRENT_DATE", sql, flags=re.IGNORECASE)

            # 3. Translate DATEDIFF(day, a, b) -> ((b) - (a))
            def replace_datediff(match):
                content = match.group(1)
                args = []
                current = []
                depth = 0
                for char in content:
                    if char == "," and depth == 0:
                        args.append("".join(current).strip())
                        current = []
                    else:
                        if char == "(":
                            depth += 1
                        elif char == ")":
                            depth -= 1
                        current.append(char)
                args.append("".join(current).strip())
                if len(args) == 3 and args[0].lower() == "day":
                    return f"(({args[2]}) - ({args[1]}))"
                return match.group(0)

            sql = re.sub(
                r"\bDATEDIFF\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)",
                replace_datediff,
                sql,
                flags=re.IGNORECASE,
            )

            # 4. Translate ISNULL(a, b) -> COALESCE(a, b)
            def replace_isnull(match):
                content = match.group(1)
                args = []
                current = []
                depth = 0
                for char in content:
                    if char == "," and depth == 0:
                        args.append("".join(current).strip())
                        current = []
                    else:
                        if char == "(":
                            depth += 1
                        elif char == ")":
                            depth -= 1
                        current.append(char)
                args.append("".join(current).strip())
                if len(args) == 2:
                    return f"COALESCE({args[0]}, {args[1]})"
                return match.group(0)

            sql = re.sub(
                r"\bISNULL\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)",
                replace_isnull,
                sql,
                flags=re.IGNORECASE,
            )

        return sql


class TemplateRegistry:
    def __init__(self, templates: dict[str, SQLTemplate]) -> None:
        self._t = templates

    def get(self, template_id: str) -> SQLTemplate:
        if template_id == "sales_overview" and template_id not in self._t:
            return SQLTemplate(
                id="sales_overview",
                description="Default Sales Overview",
                module="ar",
                category="sales_performance",
                supported_dbs=("mssql", "postgres", "cloudsql"),
                params={},
                derived_params=(),
                sql_by_dialect={
                    "mssql": "SELECT TOP 10 c.Customer, c.Name AS CustomerName, (b.CurrentBalance1 + b.CurrentBalance2 + b.CurrentBalance3) AS CurrentBalance FROM ArCustomer c LEFT JOIN ArCustomerBal b ON b.Customer = c.Customer ORDER BY CurrentBalance DESC",
                    "postgres": "SELECT 1 AS ok",
                    "cloudsql": "SELECT 1 AS ok",
                },
                result_columns=("Customer", "CustomerName", "CurrentBalance"),
                keywords=(),
                embedding_text="",
            )
        if template_id not in self._t:
            raise NotFound(f"Template not found: {template_id}")
        return self._t[template_id]

    def list_for_llm(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "description": t.description,
                "module": t.module,
                "category": t.category,
                "params": t.params,
                "supported_dbs": list(t.supported_dbs),
            }
            for t in self._t.values()
        ]

    def all(self) -> list[SQLTemplate]:
        return list(self._t.values())

    def has(self, template_id: str) -> bool:
        return template_id in self._t or template_id == "sales_overview"

    def count(self) -> int:
        return len(self._t) + (1 if "sales_overview" not in self._t else 0)


def _validate_sql(sql: str, template_id: str) -> None:
    sql_norm = sql.strip()
    if not sql_norm.upper().startswith("SELECT") and not sql_norm.upper().startswith("WITH"):
        raise ValidationFailed(f"Template {template_id}: only SELECT/CTE allowed")
    if _FORBIDDEN.search(sql_norm):
        raise ValidationFailed(f"Template {template_id}: forbidden keyword in SQL")


def load_registry(path: Path = TEMPLATES_PATH) -> TemplateRegistry:
    """Load from the original format (sql_by_dialect dict)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, SQLTemplate] = {}
    for entry in raw["templates"]:
        sql_by_dialect = entry["sql"]
        for d, q in sql_by_dialect.items():
            _validate_sql(q, entry["id"])
        t = SQLTemplate(
            id=entry["id"],
            description=entry.get("description", ""),
            module=entry.get("module", ""),
            category=entry.get("category", ""),
            supported_dbs=tuple(entry.get("supported_dbs", [])),
            params=entry.get("params", {}),
            derived_params=tuple(entry.get("derived_params", [])),
            sql_by_dialect=sql_by_dialect,
            result_columns=tuple(entry.get("result_columns", [])),
            keywords=tuple(entry.get("keywords", [])),
            embedding_text=entry.get("embedding_text", ""),
        )
        out[t.id] = t
    return TemplateRegistry(out)


def load_combined_registry(path: Path = COMBINED_TEMPLATES_PATH) -> TemplateRegistry:
    """Load from the combined format (single sql string → mssql dialect)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, SQLTemplate] = {}
    entries = raw if isinstance(raw, list) else raw.get("templates", [])
    for entry in entries:
        sql = entry.get("sql", "")
        if sql:
            _validate_sql(sql, entry["id"])
        t = SQLTemplate(
            id=entry["id"],
            description=entry.get("description", ""),
            module=entry.get("module", ""),
            category=entry.get("category", ""),
            supported_dbs=("mssql",),
            params=entry.get("params", {}),
            derived_params=tuple(entry.get("derived_params", [])),
            sql_by_dialect={"mssql": sql} if sql else {},
            result_columns=tuple(entry.get("result_columns", [])),
            keywords=tuple(entry.get("keywords", [])),
            embedding_text=entry.get("embedding_text", ""),
        )
        out[t.id] = t
    return TemplateRegistry(out)


def create_template_from_pinecone(meta: dict[str, Any]) -> SQLTemplate:
    """Build a SQLTemplate from Pinecone search result metadata."""
    params = meta.get("params", {})
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            params = {}

    result_columns = meta.get("result_columns", [])
    if isinstance(result_columns, str):
        try:
            result_columns = json.loads(result_columns)
        except json.JSONDecodeError:
            result_columns = []

    sql = meta.get("sql", "")
    return SQLTemplate(
        id=meta.get("id", ""),
        description=meta.get("description", ""),
        module=meta.get("module", ""),
        category=meta.get("category", ""),
        supported_dbs=("mssql",),
        params=params,
        derived_params=(),
        sql_by_dialect={"mssql": sql} if sql else {},
        result_columns=tuple(result_columns),
        keywords=(),
        embedding_text="",
    )


_registry: TemplateRegistry | None = None


def init_template_registry() -> TemplateRegistry:
    global _registry
    # Try combined templates (packaged first, then dev path), fall back to original
    if COMBINED_TEMPLATES_PATH_PACKAGED.exists():
        _registry = load_combined_registry(COMBINED_TEMPLATES_PATH_PACKAGED)
    elif COMBINED_TEMPLATES_PATH.exists():
        _registry = load_combined_registry(COMBINED_TEMPLATES_PATH)
    else:
        _registry = load_registry()
    return _registry


def get_template_registry() -> TemplateRegistry:
    if _registry is None:
        raise RuntimeError("Template registry not initialized")
    return _registry
