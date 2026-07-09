"""Context Builder — assembles YAML ontology + adapter data into an LLM prompt string.

Takes ontology, adapters, joins, and meta as input and produces a structured
plain-text context block that the SemanticResolver injects into its system prompt.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.engine.loader import (
    get_erp_paths,
    load_adapters,
    load_joins,
    load_meta,
    load_ontology,
)

log = get_logger(__name__)


class ContextBuilder:
    """Builds the semantic context string from YAML knowledge graph files."""

    def __init__(self, erp_type: str) -> None:
        self.erp_type = erp_type.lower().strip()
        self._paths = get_erp_paths(self.erp_type)

    # ── Public ─────────────────────────────────────────────────────────────

    def build(self) -> str:
        """Load all YAML files and return a structured prompt context string."""
        ontology = load_ontology(self._paths.ontology_dir)
        adapters = load_adapters(self._paths.adapter_dir)
        joins = load_joins(self._paths.relationship_file)
        meta = load_meta(self._paths.adapter_dir)
        return self._assemble(ontology, adapters, joins, meta)

    def load_meta(self) -> dict:
        """Expose meta for dialect detection without re-loading all files."""
        return load_meta(self._paths.adapter_dir)

    # ── Private ────────────────────────────────────────────────────────────

    def _assemble(
        self,
        ontology: dict[str, dict],
        adapters: dict[str, dict],
        joins: dict,
        meta: dict,
    ) -> str:
        ctx: list[str] = [f"ERP Type: {self.erp_type.upper()}\n"]

        # ── Schema inventory ─────────────────────────────────────────────
        tables_meta = meta.get("tables") or {}
        if tables_meta:
            ctx.append("--- COMPLETE DATABASE SCHEMA (ONLY these tables/columns exist) ---")
            for tbl_name, tbl_info in tables_meta.items():
                alias = tbl_info.get("alias", "")
                cols = ", ".join(tbl_info.get("columns", []))
                ctx.append(f"  Table: {tbl_name} (alias: {alias})  Columns: [{cols}]")
                if tbl_info.get("notes"):
                    ctx.append(f"    Notes: {tbl_info['notes']}")
            ctx.append("")

        # ── Data rules ───────────────────────────────────────────────────
        data_rules = meta.get("data_rules") or {}
        if data_rules:
            ctx.append("--- DATA RULES ---")
            for rule_name, rule_desc in data_rules.items():
                ctx.append(f"  {rule_name}: {str(rule_desc).strip()}")
            ctx.append("")

        # ── Business concepts & field mappings ───────────────────────────
        ctx.append("--- BUSINESS CONCEPTS & FIELD MAPPINGS ---")
        for concept_name, ont in ontology.items():
            adapter = adapters.get(concept_name)
            if not adapter:
                continue
            ctx.extend(self._format_concept(concept_name, ont, adapter))

        # ── Global join relationships ────────────────────────────────────
        ctx.append("\n--- JOIN RELATIONSHIPS (joins.yaml) ---")
        for rel in joins.get("relationships", []):
            ctx.append(
                f"  - {rel.get('join_type', 'LEFT')} JOIN: "
                f"{rel.get('from_concept')} -> {rel.get('to_concept')} "
                f"ON {rel.get('condition')}"
            )

        return "\n".join(ctx)

    @staticmethod
    def _format_concept(concept_name: str, ont: dict, adapter: dict) -> list[str]:
        lines: list[str] = [
            f"\nConcept: {concept_name} (Module: {ont.get('module')})",
            f"  Description: {ont.get('description', '').strip()}",
            f"  Synonyms: {', '.join(ont.get('synonyms', []))}",
        ]

        header_table = adapter.get("header_table") or adapter.get("table")
        alias = adapter.get("alias", "")
        detail_table = adapter.get("detail_table")

        if header_table:
            lines.append(f"  Primary Table: {header_table} (alias: {alias})")
        if detail_table:
            detail_alias = adapter.get("detail_alias", "")
            detail_join = adapter.get("detail_join", "")
            lines.append(f"  Detail Table: {detail_table} (alias: {detail_alias}, join: {detail_join})")

        # Fields
        lines.append("  Fields:")
        fields = adapter.get("fields") or adapter.get("header_fields") or {}
        for u_field, db_col in fields.items():
            lines.append(f"    - {u_field}: {db_col}")

        calc_fields = adapter.get("calculated_fields") or {}
        for u_field, expr in calc_fields.items():
            lines.append(f"    - {u_field} (calculated): {expr}")

        detail_fields = adapter.get("detail_fields") or {}
        if detail_fields:
            lines.append("  Detail Line Fields:")
            for u_field, db_col in detail_fields.items():
                lines.append(f"    - {u_field}: {db_col}")

        # Filters
        filters = adapter.get("filters") or adapter.get("default_filters") or {}
        if filters:
            lines.append("  Predefined Filters:")
            for fname, fexpr in filters.items():
                lines.append(f"    - {fname}: {fexpr}")

        # Joins
        adapter_joins = adapter.get("joins") or {}
        if adapter_joins:
            lines.append("  Available Joins:")
            for jname, jinfo in adapter_joins.items():
                lines.append(
                    f"    - {jname}: {jinfo.get('table')} {jinfo.get('alias', '')} "
                    f"ON {jinfo.get('on', '')}"
                )

        # Balance mapping
        bal_map = adapter.get("balance_mapping")
        if bal_map:
            lines.append(f"  Balance Table: {bal_map.get('table')} (Join: {bal_map.get('join_on')})")
            for u_field, db_col in bal_map.get("fields", {}).items():
                lines.append(f"    - {u_field}: {db_col}")

        # Sample SQL
        sample_sql = adapter.get("sample_sql")
        if sample_sql and isinstance(sample_sql, dict):
            lines.append("  Reference SQL Examples:")
            for sq_name, sq_val in sample_sql.items():
                lines.append(f"    [{sq_name}]: {str(sq_val).strip()}")
        elif sample_sql and isinstance(sample_sql, str):
            lines.append(f"  Reference SQL: {sample_sql.strip()}")

        return lines
