"""V2 semantic resolver.

Resolves universal business concepts + attribute selections into
parameterized T-SQL (SQL Server dialect) using the ERP adapters and the
predefined join graph. Values are never interpolated - only %(name)s
placeholders are emitted, matching the existing template executor's
parameter binding convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .loader import (
    Adapter,
    Concept,
    Relationship,
    SemanticLayerError,
    cross_validate,
    load_adapters,
    load_ontology,
    load_relationships,
)

_ALLOWED_OPS = {"=", "<>", ">", ">=", "<", "<=", "LIKE", "IN", "BETWEEN"}


@dataclass
class Filter:
    attribute: str
    op: str
    param: str  # parameter name, bound later by parameter_binder


@dataclass
class ConceptQuery:
    concept: str
    attributes: list[str]
    filters: list[Filter] = field(default_factory=list)
    join_to: list[str] = field(default_factory=list)  # related concepts to join
    order_by: str | None = None
    descending: bool = True
    limit_param: str = "limit"


class Resolver:
    def __init__(self, erp: str, root: str | None = None):
        kwargs = {"root": root} if root else {}
        self.erp = erp
        self.concepts: dict[str, Concept] = load_ontology(**kwargs)
        self.adapters: dict[str, Adapter] = load_adapters(erp, **kwargs)
        self.relationships: list[Relationship] = load_relationships(erp, **kwargs)
        errors = cross_validate(self.concepts, self.adapters)
        if errors:
            raise SemanticLayerError("; ".join(errors))

    # ------------------------------------------------------------------
    def resolve_concept(self, text: str) -> str | None:
        """Match free text to a concept via name, display name or synonyms."""
        needle = text.strip().lower()
        for c in self.concepts.values():
            if needle == c.name or needle == c.display_name.lower():
                return c.name
            if any(needle == s.lower() for s in c.synonyms):
                return c.name
        return None

    def field_expr(self, concept: str, attribute: str) -> str:
        adapter = self._adapter(concept)
        expr = adapter.all_fields().get(attribute)
        if expr is None:
            raise SemanticLayerError(
                f"{concept}: attribute '{attribute}' has no {self.erp} mapping"
            )
        return expr

    # ------------------------------------------------------------------
    def build_sql(self, q: ConceptQuery) -> str:
        adapter = self._adapter(q.concept)
        alias = adapter.alias or adapter.table

        select_parts = []
        for attr in q.attributes:
            expr = self.field_expr(q.concept, attr)
            select_parts.append(f"{expr} AS {attr}")
        for related in q.join_to:
            rel_adapter = self._adapter(related)
            for attr, expr in rel_adapter.fields.items():
                select_parts.append(f"{expr} AS {related}__{attr}")
        if not select_parts:
            raise SemanticLayerError("No attributes selected")

        from_clause = f"{adapter.table} {alias}" if adapter.alias else adapter.table
        joins: list[str] = []

        if adapter.balance_mapping and self._needs_balance(adapter, q.attributes):
            bm = adapter.balance_mapping
            b_alias = bm.get("alias") or bm["table"]
            joins.append(f"LEFT JOIN {bm['table']} {b_alias} ON {bm['join_on']}")

        for related in q.join_to:
            rel = self._relationship(q.concept, related)
            rel_adapter = self._adapter(related)
            r_alias = rel_adapter.alias or rel_adapter.table
            joins.append(
                f"{rel.join_type} JOIN {rel_adapter.table} {r_alias} ON {rel.condition}"
            )

        where_parts = []
        for f in q.filters:
            if f.op.upper() not in _ALLOWED_OPS:
                raise SemanticLayerError(f"Disallowed operator: {f.op!r}")
            expr = self.field_expr(q.concept, f.attribute)
            where_parts.append(f"{expr} {f.op.upper()} %({f.param})s")

        sql = f"SELECT TOP %({q.limit_param})s " + ", ".join(select_parts)
        sql += f" FROM {from_clause}"
        if joins:
            sql += " " + " ".join(joins)
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if q.order_by:
            sql += f" ORDER BY {self.field_expr(q.concept, q.order_by)}"
            sql += " DESC" if q.descending else " ASC"
        return sql

    # ------------------------------------------------------------------
    def _adapter(self, concept: str) -> Adapter:
        adapter = self.adapters.get(concept)
        if adapter is None:
            raise SemanticLayerError(
                f"Concept '{concept}' has no adapter for ERP '{self.erp}'"
            )
        return adapter

    def _relationship(self, from_c: str, to_c: str) -> Relationship:
        for rel in self.relationships:
            if rel.from_concept == from_c and rel.to_concept == to_c:
                return rel
        raise SemanticLayerError(f"No join path defined: {from_c} -> {to_c}")

    @staticmethod
    def _needs_balance(adapter: Adapter, attributes: list[str]) -> bool:
        bal_fields = set((adapter.balance_mapping or {}).get("fields", {}))
        return bool(bal_fields.intersection(attributes))
