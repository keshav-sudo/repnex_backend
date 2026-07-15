"""Predictive Analysis Service — Agentic customer, supplier, and inventory prediction engine.

Pipeline:
1. Detect if user query is asking for a prediction/forecast and identify target (customer, supplier, inventory).
2. Fetch multi-source history from the appropriate ERP tables.
3. Calculate RFM, order cycle patterns, and stock cover in Python (no hallucination).
4. Feed exact metrics to LLM for reasoned prediction narrative.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.engine import BoundQuery, execute_collect
from app.llm.client import get_llm
from app.schemas.query import ChatResponse, IntentResult
from app.services import connection_service

log = logging.getLogger(__name__)

# ── Unified Keyword-based predictive intent detection ─────────────────────────
_PREDICTIVE_PATTERNS = re.compile(
    r"""
    (
        # ── English ──
        likely\s+to\s+(order|buy|purchase|reorder|run\w*\s+out|stock\w*\s*out|need\s+payment) |
        will\s+(order|buy|purchase|run\w*\s+out|stock\w*\s*out)\s+next                        |
        predict\w*\s+(order|purchase|buy|payment|stock\w*\s*out|replenish)                  |
        next\s+(order|purchase|payment)\s+(for|from|by|to)                           |
        who\s+(will|is\s+going\s+to)\s+(order|buy|pay)                               |
        which\s+(customer|supplier|vendor|item|product|stock)\s+(will|is\s+likely)   |
        who\s+is\s+likely                                                             |
        (customer|supplier|stock|inventory|item)\s+churn\s+risk                      |
        at\s+risk\s+of\s+(churn|leaving|stock\w*\s*out)                               |
        reorder\s+probability                                                         |
        forecast\w*\s+(order|sales|payment|stock\w*\s*out)                            |
        (customer|supplier|stock|inventory|item)\w*\s+prediction                      |
        predict\s+next                                                                |
        stock\w*\s*out\s+risk                                                         |
        run\w*\s+out\s+soon                                                           |
        payment\s+predictions?                                                        |

        # ── Hinglish ──
        kaun\w*\s+(customer|client|supplier|vendor|item)\s*(aage|next|ab)?\s*(order|buy|krega|karega|dega|payment|stockout) |
        kaunsa\s*(customer|client|supplier|vendor)\s*(order|buy|payment)\s*(karega|dega|krega) |
        next\s+order\s+(dega|karega|krega)                                            |
        order\s+(prediction|predict)\s+(karo|do|chahiye)                              |
        customer\s+ki\s+prediction                                                    |
        agle\s+(order|purchase|payment)\s+(ki|ke)\s+(prediction|forecast)             |
        kab\s+(dobara|phir)\s+order\s+(karega|dega|krega)                             |
        dobara\s+order\s+(kaun|kaunsa)                                                |
        prediction\s+(do|dedo|chahiye|karo)                                           |
        kon\s+sa\s+(customer|supplier|item)\s*(order|buy|payment)\s*(karega|dega|krega|karta)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_CODE_FILTER_PATTERN = re.compile(
    r"(?:for|of|item|product|customer|client|supplier|vendor|ke\s+liye)\s+([A-Z0-9-]{3,20})\b",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "will", "is", "has", "was", "next", "the", "that", "this", "to", "from", 
    "with", "for", "not", "are", "about", "some", "what", "which", "who", 
    "whom", "karo", "do", "dedo", "chahiye", "aage", "ab", "kab", "phir",
    "ko", "se", "ka", "ki", "ke", "hi", "le", "de", "pe", "par", "ne",
    "soon", "karega", "dega", "krega", "karta", "order", "buy", "pay", "run", "out"
}


def _detect_predictive_intent(nl: str) -> tuple[bool, str, str | None]:
    """Keyword-based predictive intent detection.

    Returns:
        (is_predictive, target_type, filter_value)
        target_type: 'customer', 'supplier', or 'inventory'
    """
    is_pred = bool(_PREDICTIVE_PATTERNS.search(nl))
    if not is_pred:
        return False, "customer", None

    # Determine target type
    target_type = "customer"
    nl_lower = nl.lower()
    if any(x in nl_lower for x in ["supplier", "vendor", "apinvoice", "apsupplier", "payment"]):
        target_type = "supplier"
    elif any(
        x in nl_lower
        for x in [
            "stock",
            "inventory",
            "item",
            "product",
            "replenish",
            "run out",
            "stockout",
            "invmaster",
            "invwarehouse",
        ]
    ):
        target_type = "inventory"

    # Extract code filter
    match = _CODE_FILTER_PATTERN.search(nl)
    filter_val = None
    if match:
        candidate = match.group(1).upper()
        if candidate.lower() not in _STOP_WORDS:
            filter_val = candidate

    return True, target_type, filter_val


# ── Prompts ───────────────────────────────────────────────────────────────────

CUSTOMER_PREDICTIVE_PROMPT = """\
You are a precise business intelligence assistant. You have been given calculated
purchase pattern metrics for customers based on their real invoice history.
Your job is to generate clear, fact-based ORDER PREDICTIONS with reasoning.

Data provided:
{customer_predictions}

Guidelines:
1. For each customer, state: WHEN they are likely to order, WHY (based on their cycle),
   and HOW CONFIDENT you are (based on consistency score).
2. Rank customers from "most likely to order soon" to "least likely".
3. Use this format for each:
   🏆 **[Customer Code] — [Customer Name]**
   - 📅 Predicted Next Order: ~[date] ([N] days from now)
   - 🔁 Order Cycle: Every [N] days on average
   - 📊 Last Invoice: [N] days ago
   - 💡 Reason: [1 sentence factual reason based on pattern]
   - ✅ Confidence: High/Medium/Low ([consistency score]% consistency)
4. End with a 2-sentence summary of overall customer activity health.
5. Do NOT invent any data. Only use the numbers provided above.
Output clean Markdown only.
"""

SUPPLIER_PREDICTIVE_PROMPT = """\
You are a precise business intelligence assistant. You have been given calculated
billing and payment pattern metrics for suppliers based on real purchase invoice history.
Your job is to generate clear, fact-based SUPPLIER PAYMENT/ORDER PREDICTIONS with reasoning.

Data provided:
{supplier_predictions}

Guidelines:
1. For each supplier, state: WHEN we are likely to order/pay next, WHY (based on cycle),
   and HOW CONFIDENT you are (based on consistency score).
2. Rank suppliers from "next payment/order expected soonest" to "furthest".
3. Use this format for each:
   🏆 **[Supplier Code] — [Supplier Name]**
   - 📅 Predicted Next Order/Payment: ~[date] ([N] days from now)
   - 🔁 Order Cycle: Every [N] days on average
   - 📊 Last Invoice: [N] days ago
   - 💡 Reason: [1 sentence factual reason based on pattern]
   - ✅ Confidence: High/Medium/Low ([consistency score]% consistency)
4. End with a 2-sentence summary of overall supplier activity and cashflow outlook.
5. Do NOT invent any data. Only use the numbers provided above.
Output clean Markdown only.
"""

INVENTORY_PREDICTIVE_PROMPT = """\
You are a precise warehouse and inventory intelligence assistant. You have been given calculated
stockout risk and stock cover metrics for products based on real warehouse levels and sales velocity.
Your job is to generate clear, fact-based STOCKOUT RISK & REPLENISHMENT PREDICTIONS with reasoning.

Data provided:
{inventory_predictions}

Guidelines:
1. For each item, state: WHEN it is predicted to run out, WHY (based on current stock vs sales velocity),
   and the RISK level (Critical/High/Medium/Low).
2. Rank items from "highest/soonest stockout risk" to "lowest".
3. Use this format for each:
   📦 **[Stock Code] — [Product Description]** (Warehouse: [Warehouse])
   - 🚨 Risk Level: Critical/High/Medium/Low
   - 📊 Current Stock: [N] units (Safety Stock: [N] units, Min Stock: [N] units)
   - 📅 Days of Cover: [N] days (Predicted Stockout: ~[date])
   - 💡 Action: [1 sentence factual recommendation, e.g. reorder [N] units]
4. End with a 2-sentence summary of overall warehouse stock health and replenishment priorities.
5. Do NOT invent any data. Only use the numbers provided above.
Output clean Markdown only.
"""


# ── Main Entry Point ──────────────────────────────────────────────────────────


async def detect_and_run_predictive(
    db: AsyncIOMotorDatabase,
    current: Any,
    connection_id: uuid.UUID | None,
    natural_language: str,
) -> ChatResponse | None:
    """Detects predictive queries and executes the appropriate agentic prediction pipeline."""
    if not connection_id:
        return None

    # Step 1: Detect intent and target type
    is_predictive, target_type, filter_val = _detect_predictive_intent(natural_language)
    if not is_predictive:
        return None

    log.info(
        f"Predictive query detected. target_type={target_type}, filter_val={filter_val}"
    )

    try:
        conn = await connection_service.get_connection(db, current, connection_id)
    except Exception:
        return None

    if target_type == "customer":
        return await _run_customer_prediction(conn, filter_val)
    elif target_type == "supplier":
        return await _run_supplier_prediction(conn, filter_val)
    elif target_type == "inventory":
        return await _run_inventory_prediction(conn, filter_val)

    return None


# ── Customer Prediction Pipeline ──────────────────────────────────────────────


async def _run_customer_prediction(conn: Any, customer_filter: str | None) -> ChatResponse:
    # 1. Fetch data
    invoice_rows = await _fetch_invoice_history(conn, customer_filter)
    if not invoice_rows:
        return ChatResponse(
            type="conversational",
            message="I couldn't find any customer invoice history in the database to generate predictions.",
        )

    customer_names = await _fetch_customer_names(conn)
    balances = await _fetch_balances(conn)

    # 2. Compute predictions
    predictions = _calculate_predictions(invoice_rows, customer_names, balances)
    if not predictions:
        return ChatResponse(
            type="conversational",
            message="Not enough invoice history (minimum 2 invoices per customer) to generate order predictions.",
        )

    # 3. Explain via LLM
    prediction_text = _format_predictions_for_llm(predictions)
    try:
        ai_response = await get_llm().chat_text(
            system=CUSTOMER_PREDICTIVE_PROMPT.format(customer_predictions=prediction_text),
            user="Generate the customer order predictions now.",
        )
    except Exception as e:
        log.warning(f"predictive_customer_llm_failed: {e}")
        ai_response = prediction_text

    # 4. Format display rows
    display_rows = [
        {
            "Customer": p["customer_code"],
            "Name": p["customer_name"],
            "Last Invoice (days ago)": p["days_since_last"],
            "Avg Order Cycle (days)": round(p["avg_cycle_days"], 1),
            "Predicted Next Order": p["predicted_next_order"],
            "Confidence": p["confidence_label"],
        }
        for p in predictions[:15]
    ]

    intent = IntentResult(
        template_id="predictive_customer_analysis",
        params={"target": "customer", "customer_filter": customer_filter},
        confidence=1.0,
        rationale="agentic_rfm_prediction",
    )

    return ChatResponse(
        type="executable",
        message=ai_response,
        template_id="predictive_customer_analysis",
        template_description="Predictive Customer Order Analysis",
        template_module="predictive_engine",
        sql="-- Predictive analysis: multi-source ArInvoice + ArCustomer + ArCustomerBal",
        rows=display_rows,
        columns=[
            "Customer",
            "Name",
            "Last Invoice (days ago)",
            "Avg Order Cycle (days)",
            "Predicted Next Order",
            "Confidence",
        ],
        rows_returned=len(display_rows),
        execution_time_ms=0,
        summary=ai_response,
        suggestions=[
            "Which customers are at churn risk?",
            "Show customers who order every month",
            "Which customer hasn't ordered in 60 days?",
        ],
        intent=intent,
    )


# ── Supplier Prediction Pipeline ──────────────────────────────────────────────


async def _run_supplier_prediction(conn: Any, supplier_filter: str | None) -> ChatResponse:
    # 1. Fetch AP invoices
    invoice_rows = await _fetch_supplier_invoice_history(conn, supplier_filter)
    if not invoice_rows:
        return ChatResponse(
            type="conversational",
            message="I couldn't find any supplier invoice history in the database to generate predictions.",
        )

    supplier_names, balances = await _fetch_supplier_names_and_balances(conn)

    # 2. Compute cycles
    predictions = _calculate_supplier_predictions(invoice_rows, supplier_names, balances)
    if not predictions:
        return ChatResponse(
            type="conversational",
            message="Not enough purchase history (minimum 2 invoices per supplier) to calculate billing cycles.",
        )

    # 3. LLM explainer
    prediction_text = _format_supplier_predictions_for_llm(predictions)
    try:
        ai_response = await get_llm().chat_text(
            system=SUPPLIER_PREDICTIVE_PROMPT.format(supplier_predictions=prediction_text),
            user="Generate the supplier billing/order predictions now.",
        )
    except Exception as e:
        log.warning(f"predictive_supplier_llm_failed: {e}")
        ai_response = prediction_text

    # 4. Display rows
    display_rows = [
        {
            "Supplier": p["supplier_code"],
            "Name": p["supplier_name"],
            "Last Invoice (days ago)": p["days_since_last"],
            "Avg Billing Cycle (days)": round(p["avg_cycle_days"], 1),
            "Predicted Next Invoice": p["predicted_next_invoice"],
            "Confidence": p["confidence_label"],
        }
        for p in predictions[:15]
    ]

    intent = IntentResult(
        template_id="predictive_supplier_analysis",
        params={"target": "supplier", "supplier_filter": supplier_filter},
        confidence=1.0,
        rationale="agentic_supplier_rfm_prediction",
    )

    return ChatResponse(
        type="executable",
        message=ai_response,
        template_id="predictive_supplier_analysis",
        template_description="Predictive Supplier Billing Analysis",
        template_module="predictive_engine",
        sql="-- Predictive analysis: multi-source ApInvoice + ApSupplier",
        rows=display_rows,
        columns=[
            "Supplier",
            "Name",
            "Last Invoice (days ago)",
            "Avg Billing Cycle (days)",
            "Predicted Next Invoice",
            "Confidence",
        ],
        rows_returned=len(display_rows),
        execution_time_ms=0,
        summary=ai_response,
        suggestions=[
            "Which supplier requires payment next?",
            "Show supplier payment cycles",
            "List active suppliers with outstanding balances",
        ],
        intent=intent,
    )


# ── Inventory Prediction Pipeline ─────────────────────────────────────────────


async def _run_inventory_prediction(conn: Any, stock_filter: str | None) -> ChatResponse:
    # 1. Fetch inventory status
    inventory_rows = await _fetch_inventory_status(conn, stock_filter)
    if not inventory_rows:
        return ChatResponse(
            type="conversational",
            message="No active stock records found in the warehouse database to run predictions.",
        )

    # 2. Calculate stockout risks
    predictions = _calculate_inventory_predictions(inventory_rows)

    # 3. LLM Explainer
    prediction_text = _format_inventory_predictions_for_llm(predictions)
    try:
        ai_response = await get_llm().chat_text(
            system=INVENTORY_PREDICTIVE_PROMPT.format(inventory_predictions=prediction_text),
            user="Generate the stockout risk and replenishment predictions now.",
        )
    except Exception as e:
        log.warning(f"predictive_inventory_llm_failed: {e}")
        ai_response = prediction_text

    # 4. Display rows
    display_rows = [
        {
            "Stock Code": p["stock_code"],
            "Description": p["description"],
            "Warehouse": p["warehouse"],
            "Stock On Hand": p["qty_on_hand"],
            "Days of Cover": p["days_of_cover"],
            "Predicted Stockout Date": p["predicted_stockout"],
            "Risk": p["risk_label"],
        }
        for p in predictions[:15]
    ]

    intent = IntentResult(
        template_id="predictive_inventory_analysis",
        params={"target": "inventory", "stock_filter": stock_filter},
        confidence=1.0,
        rationale="agentic_inventory_stockout_prediction",
    )

    return ChatResponse(
        type="executable",
        message=ai_response,
        template_id="predictive_inventory_analysis",
        template_description="Predictive Inventory Stockout Analysis",
        template_module="predictive_engine",
        sql="-- Predictive analysis: InvWarehouse + InvMaster",
        rows=display_rows,
        columns=[
            "Stock Code",
            "Description",
            "Warehouse",
            "Stock On Hand",
            "Days of Cover",
            "Predicted Stockout Date",
            "Risk",
        ],
        rows_returned=len(display_rows),
        execution_time_ms=0,
        summary=ai_response,
        suggestions=[
            "Which items are at critical stockout risk?",
            "Show warehouse items with low stock cover",
            "What is the replenishment priority for warehouse?",
        ],
        intent=intent,
    )


# ── Data Fetchers ─────────────────────────────────────────────────────────────


async def _fetch_invoice_history(conn: Any, customer_filter: str | None) -> List[Dict]:
    """Fetch 12-month invoice history from ArInvoice relative to latest invoice date."""
    is_pg = conn.db_type.value in ("postgres", "cloudsql")

    # Use max invoice date as reference point so it works on historical databases
    ref_date_sql = "(SELECT COALESCE(MAX(InvoiceDate), NOW()) FROM ArInvoice)"
    if is_pg:
        date_filter = f"ai.InvoiceDate >= {ref_date_sql} - INTERVAL '365 days'"
    else:
        date_filter = f"ai.InvoiceDate >= DATE_SUB({ref_date_sql}, INTERVAL 365 DAY)"

    where_customer = ""
    if customer_filter:
        from app.engine.parameter_binder import sanitize_string

        sanitized = sanitize_string(customer_filter)
        where_customer = f" AND ai.Customer = '{sanitized}'"

    sql = f"""
    SELECT
        ai.Customer,
        ai.Invoice,
        ai.InvoiceDate,
        ai.InvoiceYear,
        ai.InvoiceMonth
    FROM ArInvoice ai
    WHERE {date_filter}{where_customer}
      AND ai.InvoiceDate IS NOT NULL
    ORDER BY ai.Customer, ai.InvoiceDate
    """

    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)
    try:
        res = await execute_collect(conn, bound)
        return res.rows or []
    except Exception as e:
        log.error(f"invoice_history_fetch_failed: {e}")
        return []


async def _fetch_customer_names(conn: Any) -> Dict[str, str]:
    """Fetch customer code → name mapping from ArCustomer."""
    sql = "SELECT Customer, Name FROM ArCustomer"
    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)
    try:
        res = await execute_collect(conn, bound)
        return {
            r.get("Customer") or r.get("customer"): r.get("Name") or r.get("name") or ""
            for r in (res.rows or [])
        }
    except Exception:
        return {}


async def _fetch_balances(conn: Any) -> Dict[str, float]:
    """Fetch outstanding balance per customer from ArCustomerBal."""
    sql = "SELECT Customer, Balance FROM ArCustomerBal"
    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)
    try:
        res = await execute_collect(conn, bound)
        return {
            r.get("Customer") or r.get("customer"): float(
                r.get("Balance") or r.get("balance") or 0
            )
            for r in (res.rows or [])
        }
    except Exception:
        return {}


async def _fetch_supplier_invoice_history(
    conn: Any, supplier_filter: str | None
) -> List[Dict]:
    """Fetch 12-month purchase invoice history from ApInvoice."""
    is_pg = conn.db_type.value in ("postgres", "cloudsql")

    ref_date_sql = "(SELECT COALESCE(MAX(InvoiceDate), NOW()) FROM ApInvoice)"
    if is_pg:
        date_filter = f"ai.InvoiceDate >= {ref_date_sql} - INTERVAL '365 days'"
    else:
        date_filter = f"ai.InvoiceDate >= DATE_SUB({ref_date_sql}, INTERVAL 365 DAY)"

    where_supplier = ""
    if supplier_filter:
        from app.engine.parameter_binder import sanitize_string

        sanitized = sanitize_string(supplier_filter)
        where_supplier = f" AND ai.Supplier = '{sanitized}'"

    sql = f"""
    SELECT
        ai.Supplier,
        ai.Invoice,
        ai.InvoiceDate,
        ai.OrigInvValue
    FROM ApInvoice ai
    WHERE {date_filter}{where_supplier}
      AND ai.InvoiceDate IS NOT NULL
    ORDER BY ai.Supplier, ai.InvoiceDate
    """

    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)
    try:
        res = await execute_collect(conn, bound)
        return res.rows or []
    except Exception as e:
        log.error(f"supplier_invoice_fetch_failed: {e}")
        return []


async def _fetch_supplier_names_and_balances(
    conn: Any,
) -> Tuple[Dict[str, str], Dict[str, float]]:
    """Fetch names and outstanding balances of active suppliers."""
    sql = "SELECT Supplier, SupplierName, CurrentBalance FROM ApSupplier"
    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)
    try:
        res = await execute_collect(conn, bound)
        names = {}
        balances = {}
        for r in res.rows or []:
            code = r.get("Supplier") or r.get("supplier")
            if code:
                names[code] = r.get("SupplierName") or r.get("suppliername") or "Unknown"
                balances[code] = float(
                    r.get("CurrentBalance") or r.get("currentbalance") or 0.0
                )
        return names, balances
    except Exception as e:
        log.error(f"supplier_names_balances_fetch_failed: {e}")
        return {}, {}


async def _fetch_inventory_status(conn: Any, stock_filter: str | None) -> List[Dict]:
    """Fetch warehouse status joined with item master details."""
    where_stock = ""
    if stock_filter:
        from app.engine.parameter_binder import sanitize_string

        sanitized = sanitize_string(stock_filter)
        where_stock = f" AND iw.StockCode = '{sanitized}'"

    sql = f"""
    SELECT
        iw.StockCode,
        im.Description,
        iw.Warehouse,
        iw.QtyOnHand,
        iw.MinimumQty,
        iw.SafetyStockQty,
        iw.UnitCost,
        iw.YtdQtySold,
        iw.PrevYearQtySold,
        iw.DateLastSale
    FROM InvWarehouse iw
    LEFT JOIN InvMaster im ON iw.StockCode = im.StockCode
    WHERE iw.QtyOnHand IS NOT NULL {where_stock}
    """

    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)
    try:
        res = await execute_collect(conn, bound)
        return res.rows or []
    except Exception as e:
        log.error(f"inventory_status_fetch_failed: {e}")
        return []


# ── Python Math Engines ───────────────────────────────────────────────────────


def _calculate_predictions(
    rows: List[Dict],
    customer_names: Dict[str, str],
    balances: Dict[str, float],
) -> List[Dict]:
    """Core RFM + order cycle calculation for customers."""
    customer_invoices: Dict[str, List[date]] = {}
    for r in rows:
        cust = r.get("Customer") or r.get("customer")
        raw_date = r.get("InvoiceDate") or r.get("invoicedate")
        if not cust or not raw_date:
            continue
        inv_date = _parse_date(raw_date)
        if inv_date:
            customer_invoices.setdefault(cust, []).append(inv_date)

    all_dates = [d for dates in customer_invoices.values() for d in dates]
    today = max(all_dates) if all_dates else date.today()
    predictions = []

    for cust, dates in customer_invoices.items():
        dates_sorted = sorted(set(dates))

        if len(dates_sorted) < 2:
            continue

        last_invoice_date = dates_sorted[-1]
        days_since_last = (today - last_invoice_date).days

        total_invoices = len(dates_sorted)
        gaps = [
            (dates_sorted[i + 1] - dates_sorted[i]).days
            for i in range(len(dates_sorted) - 1)
        ]
        avg_cycle = mean(gaps)
        cycle_std = stdev(gaps) if len(gaps) > 1 else 0.0

        if avg_cycle > 0:
            consistency_pct = max(0, round((1 - (cycle_std / avg_cycle)) * 100, 1))
        else:
            consistency_pct = 0.0

        days_until_next = max(0, round(avg_cycle - days_since_last))
        predicted_date = today + timedelta(days=days_until_next)

        if consistency_pct >= 70:
            confidence = "High"
        elif consistency_pct >= 40:
            confidence = "Medium"
        else:
            confidence = "Low"

        is_churn_risk = days_since_last > (avg_cycle * 2)

        predictions.append({
            "customer_code": cust,
            "customer_name": customer_names.get(cust, "Unknown"),
            "outstanding_balance": balances.get(cust, 0.0),
            "days_since_last": days_since_last,
            "total_invoices": total_invoices,
            "avg_cycle_days": avg_cycle,
            "cycle_std_days": round(cycle_std, 1),
            "consistency_pct": consistency_pct,
            "confidence_label": confidence,
            "days_until_next": days_until_next,
            "predicted_next_order": predicted_date.strftime("%Y-%m-%d"),
            "is_churn_risk": is_churn_risk,
            "last_invoice_date": last_invoice_date.strftime("%Y-%m-%d"),
        })

    predictions.sort(key=lambda x: (x["days_until_next"], -x["consistency_pct"]))
    return predictions


def _calculate_supplier_predictions(
    rows: List[Dict],
    supplier_names: Dict[str, str],
    balances: Dict[str, float],
) -> List[Dict]:
    """Core billing cycle prediction for suppliers."""
    supplier_invoices: Dict[str, List[date]] = {}
    for r in rows:
        sup = r.get("Supplier") or r.get("supplier")
        raw_date = r.get("InvoiceDate") or r.get("invoicedate")
        if not sup or not raw_date:
            continue
        inv_date = _parse_date(raw_date)
        if inv_date:
            supplier_invoices.setdefault(sup, []).append(inv_date)

    all_dates = [d for dates in supplier_invoices.values() for d in dates]
    today = max(all_dates) if all_dates else date.today()
    predictions = []

    for sup, dates in supplier_invoices.items():
        dates_sorted = sorted(set(dates))

        if len(dates_sorted) < 2:
            continue

        last_invoice_date = dates_sorted[-1]
        days_since_last = (today - last_invoice_date).days

        total_invoices = len(dates_sorted)
        gaps = [
            (dates_sorted[i + 1] - dates_sorted[i]).days
            for i in range(len(dates_sorted) - 1)
        ]
        avg_cycle = mean(gaps)
        cycle_std = stdev(gaps) if len(gaps) > 1 else 0.0

        if avg_cycle > 0:
            consistency_pct = max(0, round((1 - (cycle_std / avg_cycle)) * 100, 1))
        else:
            consistency_pct = 0.0

        days_until_next = max(0, round(avg_cycle - days_since_last))
        predicted_date = today + timedelta(days=days_until_next)

        if consistency_pct >= 70:
            confidence = "High"
        elif consistency_pct >= 40:
            confidence = "Medium"
        else:
            confidence = "Low"

        predictions.append({
            "supplier_code": sup,
            "supplier_name": supplier_names.get(sup, "Unknown"),
            "outstanding_balance": balances.get(sup, 0.0),
            "days_since_last": days_since_last,
            "total_invoices": total_invoices,
            "avg_cycle_days": avg_cycle,
            "cycle_std_days": round(cycle_std, 1),
            "consistency_pct": consistency_pct,
            "confidence_label": confidence,
            "days_until_next": days_until_next,
            "predicted_next_invoice": predicted_date.strftime("%Y-%m-%d"),
            "last_invoice_date": last_invoice_date.strftime("%Y-%m-%d"),
        })

    predictions.sort(key=lambda x: (x["days_until_next"], -x["consistency_pct"]))
    return predictions


def _calculate_inventory_predictions(rows: List[Dict]) -> List[Dict]:
    """Core stockout risk & replenishment velocity calculation for inventory."""
    predictions = []
    today = date.today()

    for r in rows:
        stock_code = r.get("StockCode") or r.get("stockcode")
        desc = r.get("Description") or r.get("description") or "Unknown Product"
        warehouse = r.get("Warehouse") or r.get("warehouse") or "Main"

        if not stock_code:
            continue

        qty_on_hand = float(r.get("QtyOnHand") or r.get("qtyonhand") or 0.0)
        min_qty = float(r.get("MinimumQty") or r.get("minimumqty") or 0.0)
        safety_qty = float(r.get("SafetyStockQty") or r.get("safetystockqty") or 0.0)
        unit_cost = float(r.get("UnitCost") or r.get("unitcost") or 0.0)

        ytd_sold = float(r.get("YtdQtySold") or r.get("ytdqtysold") or 0.0)
        prev_sold = float(r.get("PrevYearQtySold") or r.get("prevyearqtysold") or 0.0)

        # Sales velocity (daily run rate)
        total_sold = ytd_sold if ytd_sold > 0 else prev_sold
        daily_sales_rate = total_sold / 365.0

        # Small fallback rate to prevent division-by-zero on slow-moving items
        if daily_sales_rate <= 0:
            daily_sales_rate = 0.05

        days_of_cover = qty_on_hand / daily_sales_rate
        predicted_stockout = today + timedelta(days=min(365, max(0, int(days_of_cover))))

        # Risk Tiering
        if qty_on_hand <= safety_qty:
            risk_label = "Critical"
        elif qty_on_hand <= min_qty:
            risk_label = "High"
        elif days_of_cover < 30:
            risk_label = "Medium"
        else:
            risk_label = "Low"

        # Action plan reorder recommendations
        reorder_recommendation = max(
            0.0, round((min_qty * 1.5) - qty_on_hand) if min_qty > 0 else 50.0
        )

        predictions.append({
            "stock_code": stock_code,
            "description": desc,
            "warehouse": warehouse,
            "qty_on_hand": qty_on_hand,
            "min_qty": min_qty,
            "safety_qty": safety_qty,
            "unit_cost": unit_cost,
            "days_of_cover": round(days_of_cover, 1),
            "predicted_stockout": predicted_stockout.strftime("%Y-%m-%d"),
            "risk_label": risk_label,
            "reorder_rec": reorder_recommendation,
        })

    # Rank Critical and High risk items soonest stockout first
    predictions.sort(
        key=lambda x: (
            0 if x["risk_label"] == "Critical" else 1 if x["risk_label"] == "High" else 2,
            x["days_of_cover"],
        )
    )
    return predictions


# ── Formatters ────────────────────────────────────────────────────────────────


def _format_predictions_for_llm(predictions: List[Dict]) -> str:
    """Format customer predictions for LLM explainer."""
    lines = ["CUSTOMER ORDER PREDICTIONS:\n"]
    for i, p in enumerate(predictions[:10], 1):
        churn_note = " ⚠️ CHURN RISK" if p["is_churn_risk"] else ""
        lines.append(
            f"{i}. Customer: {p['customer_code']} ({p['customer_name']}){churn_note}\n"
            f"   - Last Invoice: {p['last_invoice_date']} ({p['days_since_last']} days ago)\n"
            f"   - Total Invoices: {p['total_invoices']}\n"
            f"   - Avg Order Cycle: {p['avg_cycle_days']:.1f} days "
            f"(±{p['cycle_std_days']} days std dev)\n"
            f"   - Consistency Score: {p['consistency_pct']}%\n"
            f"   - Days Until Predicted Next Order: {p['days_until_next']} days "
            f"({p['predicted_next_order']})\n"
            f"   - Outstanding Balance: ${p['outstanding_balance']:,.2f}\n"
        )
    return "\n".join(lines)


def _format_supplier_predictions_for_llm(predictions: List[Dict]) -> str:
    """Format supplier predictions for LLM explainer."""
    lines = ["SUPPLIER BILLING PREDICTIONS:\n"]
    for i, p in enumerate(predictions[:10], 1):
        lines.append(
            f"{i}. Supplier: {p['supplier_code']} ({p['supplier_name']})\n"
            f"   - Last Invoice: {p['last_invoice_date']} ({p['days_since_last']} days ago)\n"
            f"   - Total Invoices: {p['total_invoices']}\n"
            f"   - Avg Billing Cycle: {p['avg_cycle_days']:.1f} days "
            f"(±{p['cycle_std_days']} days std dev)\n"
            f"   - Consistency Score: {p['consistency_pct']}%\n"
            f"   - Days Until Predicted Next Invoice: {p['days_until_next']} days "
            f"({p['predicted_next_invoice']})\n"
            f"   - Outstanding Balance: ${p['outstanding_balance']:,.2f}\n"
        )
    return "\n".join(lines)


def _format_inventory_predictions_for_llm(predictions: List[Dict]) -> str:
    """Format inventory predictions for LLM explainer."""
    lines = ["INVENTORY STOCKOUT RISK PREDICTIONS:\n"]
    for i, p in enumerate(predictions[:10], 1):
        lines.append(
            f"{i}. Product: {p['stock_code']} - {p['description']} (Warehouse: {p['warehouse']})\n"
            f"   - Current Qty: {p['qty_on_hand']} units (Safety: {p['safety_qty']} units, Min: {p['min_qty']} units)\n"
            f"   - Days of Cover: {p['days_of_cover']} days\n"
            f"   - Predicted Stockout Date: {p['predicted_stockout']}\n"
            f"   - Risk Level: {p['risk_label']}\n"
            f"   - Suggested Reorder Quantity: {p['reorder_rec']} units\n"
        )
    return "\n".join(lines)


def _parse_date(raw: Any) -> Optional[date]:
    """Parse various date formats into a Python date object."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(
                    raw[:10], fmt[:8] if len(fmt) > 8 else fmt
                ).date()
            except ValueError:
                continue
    return None
