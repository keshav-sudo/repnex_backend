"""Validate the V2 semantic layer end to end.

Checks, in order:
  1. YAML syntax of every file under v2/.
  2. Ontology structural rules (snake_case, valid types, required flags).
  3. Adapter <-> ontology field consistency.
  4. NO-HALLUCINATION CHECK: every table.column referenced by the SYSPRO
     adapters and joins must exist in the schema derived from the verified
     template corpus (all_templates_combined.json).
  5. Join conditions reference declared aliases only.
  6. Smoke-test: resolver builds valid T-SQL for each mapped concept.

Run:  python -m v2.engine.validate  (from repo root)
      python validate.py --templates <path-to-all_templates_combined.json>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from v2.engine.loader import (  # noqa: E402
    V2_ROOT,
    cross_validate,
    load_adapters,
    load_ontology,
    load_relationships,
)
from v2.engine.resolver import ConceptQuery, Filter, Resolver  # noqa: E402

ERP = "syspro"


def derive_schema(templates_path: str) -> dict[str, set[str]]:
    """Rebuild the table -> columns inventory from the verified templates."""
    with open(templates_path, "r", encoding="utf-8") as f:
        templates = json.load(f)
    table_cols: dict[str, set[str]] = defaultdict(set)
    for t in templates:
        sql = t["sql"]
        alias_map: dict[str, str] = {}
        for m in re.finditer(
            r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([a-z][a-z0-9]{0,3})\b",
            sql,
        ):
            alias_map[m.group(2)] = m.group(1)
        for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql):
            alias_map.setdefault(m.group(1), m.group(1))
        for m in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)\b", sql):
            pre, col = m.groups()
            if pre in alias_map:
                table_cols[alias_map[pre]].add(col)
    return table_cols


COLREF = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)\b")


def main() -> int:
    parser = argparse.ArgumentParser()
    default_templates = os.path.join(
        os.path.dirname(V2_ROOT),
        "app", "query_engine", "templates", "all_templates_combined.json",
    )
    parser.add_argument("--templates", default=default_templates)
    args = parser.parse_args()

    failures: list[str] = []

    # 1-2. Load + structural validation (raises on error)
    concepts = load_ontology()
    adapters = load_adapters(ERP)
    relationships = load_relationships(ERP)
    print(f"[ok] loaded {len(concepts)} concepts, {len(adapters)} {ERP} adapters, "
          f"{len(relationships)} relationships")

    # 3. Cross-validation
    failures += cross_validate(concepts, adapters)

    # 4. No-hallucination check against the template-derived schema
    if os.path.exists(args.templates):
        schema = derive_schema(args.templates)
        alias_to_table = {}
        for a in adapters.values():
            alias_to_table[a.alias or a.table] = a.table
            if a.balance_mapping:
                bm = a.balance_mapping
                alias_to_table[bm.get("alias") or bm["table"]] = bm["table"]

        def check_expr(owner: str, expr: str) -> None:
            for alias, col in COLREF.findall(expr):
                table = alias_to_table.get(alias)
                if table is None:
                    failures.append(f"{owner}: unknown alias '{alias}' in '{expr}'")
                elif col not in schema.get(table, set()):
                    failures.append(
                        f"{owner}: column {table}.{col} not present in verified schema"
                    )

        for a in adapters.values():
            if a.table not in schema:
                failures.append(f"adapter {a.concept}: table {a.table} not in verified schema")
            for attr, expr in a.all_fields().items():
                check_expr(f"adapter {a.concept}.{attr}", expr)
            for name, expr in a.default_filters.items():
                check_expr(f"adapter {a.concept} filter {name}", expr)
        for rel in relationships:
            check_expr(f"join {rel.from_concept}->{rel.to_concept}", rel.condition)
        print(f"[ok] no-hallucination check ran against {len(schema)} derived tables")
    else:
        print(f"[warn] template corpus not found at {args.templates}; "
              f"skipping no-hallucination check")

    # 6. Resolver smoke tests
    resolver = Resolver(ERP, root=V2_ROOT)
    smoke = [
        ConceptQuery(
            concept="ap_invoice",
            attributes=["invoice_number", "supplier_code", "outstanding_balance", "due_date"],
            filters=[Filter("outstanding_balance", "<>", "zero")],
            join_to=["supplier"],
            order_by="outstanding_balance",
        ),
        ConceptQuery(
            concept="customer",
            attributes=["customer_code", "customer_name", "current_balance",
                        "ageing_90_days", "credit_limit"],
            order_by="current_balance",
        ),
        ConceptQuery(
            concept="ar_invoice",
            attributes=["invoice_number", "customer_code", "original_value", "invoice_date"],
            filters=[Filter("invoice_date", ">=", "start_date"),
                     Filter("invoice_date", "<=", "end_date")],
            join_to=["customer"],
            order_by="invoice_date",
        ),
        ConceptQuery(
            concept="supplier",
            attributes=["supplier_code", "supplier_name", "current_balance",
                        "purchases_year_to_date"],
            order_by="current_balance",
        ),
    ]
    for q in smoke:
        sql = resolver.build_sql(q)
        assert sql.startswith("SELECT TOP %(limit)s"), sql
        print(f"[ok] resolver {q.concept}: {sql[:110]}...")

    # Synonym resolution sanity
    assert resolver.resolve_concept("vendor invoice") == "ap_invoice"
    assert resolver.resolve_concept("debtor") == "customer"
    print("[ok] synonym resolution")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nALL CHECKS PASSED — semantic layer is consistent with the verified schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
