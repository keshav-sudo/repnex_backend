"""Shared helpers for the query service package."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.core.logging import get_logger
from app.core.security.auth import CurrentUser
from dateutil.relativedelta import relativedelta

log = get_logger(__name__)

# ── ERP type detection ─────────────────────────────────────────────────────


def determine_erp_type(conn: Any | None, org: dict | None) -> str:
    """Dynamically determine ERP type from connection and organisation metadata."""
    if conn:
        db_type_str = str(getattr(conn, "db_type", "")).lower()
        if "postgres" in db_type_str or "supabase" in db_type_str:
            return "helios"
        conn_name = getattr(conn, "name", "").lower()
        if "helios" in conn_name or "supabase" in conn_name or "postgres" in conn_name:
            return "helios"
        if "epicor" in conn_name:
            return "epicor"
        if "syspro" in conn_name:
            return "syspro"

    if org:
        system_name = org.get("erpSystem", "").lower()
        if "helios" in system_name or "supabase" in system_name or "postgres" in system_name:
            return "helios"
        if "epicor" in system_name:
            return "epicor"
        if "syspro" in system_name:
            return "syspro"

    return "syspro"


# ── Date range helpers ─────────────────────────────────────────────────────

_DATE_INDICATORS_NL = [
    "last ", "ytd", "year to date", "this year", "this month",
    "this quarter", "date range", "between", "month to date",
    "mtd", "quarter to date", "qtd",
]
_DATE_INDICATORS_SQL = ["dateadd", "datefromparts", "date_add", "date_format"]
_DATE_COLUMNS = [
    "invoicedate", "duedate", "journaldate", "chequedate",
    "paymentdate", "lastpurchdate",
]


def detect_date_dependency(sql: str, nl: str) -> bool:
    """Return True when the query implies a date range filter."""
    sql_lower = sql.lower()
    nl_lower = nl.lower()
    if any(ind in nl_lower for ind in _DATE_INDICATORS_NL):
        return True
    if any(ind in sql_lower for ind in _DATE_INDICATORS_SQL):
        return True
    for col in _DATE_COLUMNS:
        if col in sql_lower and re.search(rf"\b{col}\s*(>=|<=|>|<|between)\b", sql_lower):
            return True
    return False


def resolve_relative_date_range(nl: str) -> tuple[str | None, str | None]:
    """Parse relative date phrases and return (start, end) as ISO-8601 strings."""
    nl_lower = nl.lower().replace(",", "")
    today = date.today()

    m = re.search(r"\b(?:last|past|next|over the last|over the past)\s+(\d+)\s+months?\b", nl_lower)
    if m:
        start = today - relativedelta(months=int(m.group(1)))
        return start.isoformat(), today.isoformat()

    if re.search(r"\b(?:last|past)\s+month\b", nl_lower):
        return (today - relativedelta(months=1)).isoformat(), today.isoformat()

    m = re.search(r"\b(?:last|past|over the last|over the past)\s+(\d+)\s+days?\b", nl_lower)
    if m:
        start = today - relativedelta(days=int(m.group(1)))
        return start.isoformat(), today.isoformat()

    m = re.search(r"\b(?:last|past|over the last|over the past)\s+(\d+)\s+years?\b", nl_lower)
    if m:
        start = today - relativedelta(years=int(m.group(1)))
        return start.isoformat(), today.isoformat()

    if any(k in nl_lower for k in ("this year", "ytd", "year to date")):
        return date(today.year, 1, 1).isoformat(), today.isoformat()

    if any(k in nl_lower for k in ("this month", "mtd", "month to date")):
        return date(today.year, today.month, 1).isoformat(), today.isoformat()

    if any(k in nl_lower for k in ("this quarter", "qtd", "quarter to date")):
        qm = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, qm, 1).isoformat(), today.isoformat()

    return None, None


# ── Module detection & RBAC ────────────────────────────────────────────────

_MODULE_KEYWORDS: dict[str, list[str]] = {
    "ap": ["supplier", "vendor", "payable", "ap invoice", "ap ageing", "purchase order", "po "],
    "ar": ["customer", "receivable", "ar invoice", "ar ageing", "debtor", "outstanding"],
    "gl": ["general ledger", "gl", "journal", "account", "trial balance", "budget"],
    "inv": ["inventory", "stock", "warehouse", "item", "sku", "product", "bin"],
    "wip": ["work in progress", "wip", "job", "operation", "labour"],
    "so": ["sales order", "so ", "order line", "dispatch", "shipment"],
    "sales": ["sales", "revenue", "turnover"],
}


def detect_module_from_query(query: str) -> str | None:
    """Heuristic: return the most likely ERP module keyword for a NL query."""
    q = query.lower()
    for module, keywords in _MODULE_KEYWORDS.items():
        if any(k in q for k in keywords):
            return module
    return None


_VIEWER_ALLOWED_MODULES = {"ar", "sales", "inv"}
_ANALYST_BLOCKED_MODULES: set[str] = set()


def check_module_access(module: str | None, current: CurrentUser) -> tuple[bool, str]:
    """RBAC guard — returns (is_allowed, deny_message)."""
    role = getattr(current, "role", "viewer")
    if role == "admin":
        return True, ""
    if role == "viewer" and module and module not in _VIEWER_ALLOWED_MODULES:
        return False, (
            f"Your viewer role does not have access to the **{module.upper()}** module. "
            "Contact your administrator to request access."
        )
    return True, ""


# Level 1 mapping: sub-module key -> parent module name
MODULE_KEY_TO_PARENT: dict[str, str] = {
    # Finance sub-modules
    "ap":            "finance",
    "ar":            "finance",
    "gl":            "finance",
    # Sales sub-modules
    "so":            "sales",
    # Purchase sub-modules
    "po":            "purchase",
    # Inventory
    "inventory":     "inventory",
    # Manufacturing sub-modules
    "manufacturing": "manufacturing",
    "bom":           "manufacturing",
    "wip":           "manufacturing",
    "jobcosting":    "manufacturing",
}

