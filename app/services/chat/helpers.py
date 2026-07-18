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
        conn_name = str(getattr(conn, "name", "")).lower()
        
        # Check if it matches Helios ERP (either named helios or supabase adapter)
        if "helios" in conn_name or "supabase" in conn_name:
            return "helios"
        if "epicor" in conn_name:
            return "epicor"
        if "syspro" in conn_name:
            return "syspro"
        
        # If it doesn't match any predefined templates, treat as a dynamic user connection (UUID)
        conn_id = getattr(conn, "id", None)
        if conn_id:
            return str(conn_id)

    if org:
        system_name = org.get("erpSystem", "").lower()
        if "helios" in system_name or "supabase" in system_name:
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


# Detected module -> (Parent Module ID, Submodule ID)
DETECTION_TO_MODULE_IDS: dict[str, tuple[str, str | None]] = {
    "ap": ("finance", "ap"),
    "ar": ("finance", "ar"),
    "gl": ("finance", "gl"),
    "inv": ("inventory", "invvaluation"),
    "wip": ("manufacturing", "wip"),
    "so": ("sales", "sorder"),
    "sales": ("sales", "sinvoice"),
}

DEFAULT_MODULE_ROLES: dict[str, list[str]] = {
    "finance": ["admin", "editor", "viewer"],
    "sales": ["admin", "editor", "viewer"],
    "purchase": ["admin", "editor"],
    "manufacturing": ["admin", "editor"],
    "inventory": ["admin", "editor", "viewer"],
}


def check_module_access(module: str | None, current: CurrentUser) -> tuple[bool, str]:
    """RBAC and module-level permission guard."""
    role = getattr(current, "role", "viewer")
    
    # Admins have access to everything
    if role == "admin":
        return True, ""
        
    # If no module detected, allow by default
    if not module:
        return True, ""
        
    ids = DETECTION_TO_MODULE_IDS.get(module)
    if not ids:
        return True, ""
        
    parent_id, sub_id = ids
    perms = getattr(current, "module_permissions", None) or {}
    
    # 1. Determine parent module access
    if parent_id in perms:
        parent_allowed = bool(perms[parent_id])
    else:
        # Default fallback by role
        parent_allowed = role in DEFAULT_MODULE_ROLES.get(parent_id, [])
        
    # 2. Determine submodule access (inheriting from parent if not overridden)
    if sub_id and sub_id in perms:
        allowed = bool(perms[sub_id])
    else:
        allowed = parent_allowed
        
    if not allowed:
        module_name = parent_id.upper()
        if sub_id:
            module_name += f" ({sub_id.upper()})"
        return False, (
            f"Your role or account permissions do not have access to the **{module_name}** module. "
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

