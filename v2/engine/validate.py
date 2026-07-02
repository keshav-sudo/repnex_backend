"""Validate the V2 semantic layer end to end against the SysproEdu2 schema.

Checks, in order:
  1. YAML syntax of every file under v2/.
  2. Ontology structural rules (snake_case, valid types, required flags).
  3. Adapter <-> ontology field consistency.
  4. NO-HALLUCINATION CHECK: every table.column referenced by adapters,
     calculated fields, default filters and join conditions must exist in
     v2/schema/syspro_schema.json (transcribed from the customer-provided
     INFORMATION_SCHEMA extract; InvMaster/InvWarehouse subset verified
     from the working template corpus).
  5. Resolver smoke tests: build T-SQL for each concept, including joined
     and balance-mapped queries.
  6. Informational audit: report legacy template columns that do NOT exist
     in the schema (does not fail the build).

Run from repo root:  python v2/engine/validate.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

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
COLREF = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)\b")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--templates",
        default=os.path.join(os.path.dirname(V2_ROOT), "app", "query_engine",
                             "templates", "all_templates_combined.json"),
    )
    args = parser.parse_args()

    with open(os.path.join(V2_ROOT, "schema", "syspro_schema.json"), encoding="utf-8") as f:
        schema = {t: set(v["columns"]) for t, v in json.load(f).items()}

    failures: list[str] = []

    concepts = load_ontology()
    adapters = load_adapters(ERP)
    relationships = load_relationships(ERP)
    print(f"[ok] loaded {len(concepts)} concepts, {len(adapters)} {ERP} adapters, "
          f"{len(relationships)} relationships")
    n_attrs = sum(len(c.header_attributes) + len(c.detail_attributes) for c in concepts.values())
    print(f"[ok] {n_attrs} ontology attributes declared")

    failures += cross_validate(concepts, adapters)

    alias_to_table: dict[str, str] = {}
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
                failures.append(f"{owner}: column {table}.{col} not present in schema")

    n_checked = 0
    for a in adapters.values():
        if a.table not in schema:
            failures.append(f"adapter {a.concept}: table {a.table} not in schema")
        for attr, expr in a.all_fields().items():
            check_expr(f"adapter {a.concept}.{attr}", expr)
            n_checked += 1
        for fname, fexpr in a.default_filters.items():
            check_expr(f"adapter {a.concept} filter {fname}", fexpr)
    for rel in relationships:
        check_expr(f"join {rel.from_concept}->{rel.to_concept}", rel.condition)
    print(f"[ok] no-hallucination check: {n_checked} field expressions verified "
          f"against {len(schema)} schema tables")

    resolver = Resolver(ERP, root=V2_ROOT)
    smoke = [
        ConceptQuery("ap_invoice",
                     ["invoice_number", "supplier_code", "outstanding_balance", "due_date"],
                     filters=[Filter("outstanding_balance", "<>", "zero")],
                     join_to=["supplier"], order_by="outstanding_balance"),
        ConceptQuery("supplier",
                     ["supplier_code", "supplier_name", "current_balance",
                      "purchases_year_to_date", "prior_year_total_value"],
                     order_by="current_balance"),
        ConceptQuery("ar_invoice",
                     ["invoice_number", "customer_code", "signed_invoice_value",
                      "outstanding_balance", "invoice_date"],
                     filters=[Filter("invoice_date", ">=", "start_date"),
                              Filter("invoice_date", "<=", "end_date")],
                     join_to=["customer"], order_by="invoice_date"),
        ConceptQuery("customer",
                     ["customer_code", "customer_name", "current_balance", "credit_limit",
                      "ageing_current", "ageing_30_days", "ageing_60_days",
                      "ageing_90_days", "ageing_120_days"],
                     order_by="current_balance"),
        ConceptQuery("gl_transaction",
                     ["gl_account", "gl_year", "gl_period", "journal", "journal_date",
                      "transaction_value", "reference"],
                     filters=[Filter("gl_year", "=", "year"),
                              Filter("gl_period", "=", "period")],
                     join_to=["supplier"], order_by="transaction_value"),
        ConceptQuery("inventory_item",
                     ["stock_code", "description", "warehouse", "quantity_on_hand",
                      "quantity_available", "stock_value"],
                     order_by="stock_value"),
    ]
    for q in smoke:
        sql = resolver.build_sql(q)
        assert sql.startswith("SELECT TOP %(limit)s"), sql
        print(f"[ok] resolver {q.concept}: {sql[:100]}...")

    assert resolver.resolve_concept("vendor invoice") == "ap_invoice"
    assert resolver.resolve_concept("debtor") == "customer"
    assert resolver.resolve_concept("journal entry") == "gl_transaction"
    assert resolver.resolve_concept("sku") == "inventory_item"
    print("[ok] synonym resolution")

    # Informational: audit legacy templates against the schema
    if os.path.exists(args.templates):
        with open(args.templates, encoding="utf-8") as f:
            templates = json.load(f)
        missing: dict[str, int] = {}
        for t in templates:
            sql = t["sql"]
            amap: dict[str, str] = {}
            for m in re.finditer(
                r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([a-z][a-z0-9]{0,3})\b", sql
            ):
                if m.group(1) in schema:
                    amap[m.group(2)] = m.group(1)
            for alias, col in COLREF.findall(sql):
                tbl = amap.get(alias)
                if tbl and col not in schema[tbl]:
                    missing[f"{tbl}.{col}"] = missing.get(f"{tbl}.{col}", 0) + 1
        if missing:
            print("\n[warn] legacy templates reference columns NOT in the schema "
                  "(these templates will fail at runtime against SysproEdu2):")
            for ref, n in sorted(missing.items(), key=lambda x: -x[1]):
                print(f"       {ref}  ({n} templates)")

    if failures:
        print("\nFAILURES:")
        for f_ in failures:
            print("  -", f_)
        return 1
    print("\nALL CHECKS PASSED — semantic layer is consistent with the SysproEdu2 schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
