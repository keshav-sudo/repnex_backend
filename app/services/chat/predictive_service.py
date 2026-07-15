"""Predictive Analysis Service — Agentic customer order prediction engine.

Pipeline:
1. Detect if user query is asking for a prediction/forecast
2. Fetch multi-source invoice history from ArInvoice + ArCustomer + ArCustomerBal
3. Calculate RFM metrics + order cycle patterns in Python (no hallucination)
4. Feed exact metrics to LLM for reasoned prediction narrative
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

# ── Keyword-based predictive intent detection ─────────────────────────────────
# Covers English, Hinglish, and mixed-language queries.
# No extra LLM call needed — fast, deterministic, never silently fails.

_PREDICTIVE_PATTERNS = re.compile(
    r"""
    (
        # ── English ──
        likely\s+to\s+(order|buy|purchase|reorder)          |
        will\s+(order|buy|purchase)\s+next                  |
        predict\w*\s+(order|purchase|buy)                   |
        next\s+(order|purchase)\s+(for|from|by)             |
        who\s+(will|is\s+going\s+to)\s+(order|buy)         |
        which\s+customer\s+(will|is\s+likely|might)         |
        who\s+is\s+likely                                   |
        customer\s+going\s+to\s+(buy|order)                |
        churn\s+risk                                         |
        at\s+risk\s+of\s+(churn|leaving)                   |
        reorder\s+probability                                |
        forecast\w*\s+order                                  |
        customer\w*\s+prediction                             |
        predict\s+next                                       |

        # ── Hinglish ──
        kaun\w*\s+(customer|client)\s*(aage|next|ab)?\s*(order|buy|krega|karega|dega)  |
        kaunsa\s*(customer|client)\s*(order|buy)\s*(karega|dega|krega)                 |
        next\s+order\s+(dega|karega|krega)                                             |
        order\s+(prediction|predict)\s+(karo|do|chahiye)                               |
        customer\s+ki\s+prediction                                                     |
        agle\s+(order|purchase)\s+(ki|ke)\s+(prediction|forecast)                      |
        kab\s+(dobara|phir)\s+order\s+(karega|dega|krega)                              |
        dobara\s+order\s+(kaun|kaunsa)                                                  |
        prediction\s+(do|dedo|chahiye|karo)                                             |
        kon\s+sa\s+customer\s+(order|buy)\s*(karega|dega|krega|karta)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_CUSTOMER_FILTER_PATTERN = re.compile(
    r"(?:for|of|customer|client|ke\s+liye)\s+([A-Z0-9]{4,20})\b",
    re.IGNORECASE,
)


def _detect_predictive_intent(nl: str) -> tuple[bool, str | None]:
    """Keyword-based predictive intent detection. Returns (is_predictive, customer_code_or_None)."""
    is_pred = bool(_PREDICTIVE_PATTERNS.search(nl))
    customer_match = _CUSTOMER_FILTER_PATTERN.search(nl)
    customer_filter = customer_match.group(1).upper() if customer_match else None
    return is_pred, customer_filter


PREDICTIVE_EXPLAINER_PROMPT = """\
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

# ── Main Entry Point ──────────────────────────────────────────────────────────

async def detect_and_run_predictive(
    db: AsyncIOMotorDatabase,
    current: Any,
    connection_id: uuid.UUID | None,
    natural_language: str,
) -> ChatResponse | None:
    """Detects predictive queries and executes agentic prediction pipeline."""
    if not connection_id:
        return None

    # Step 1: Keyword-based detection — fast, no LLM call, works in any language
    is_predictive, customer_filter = _detect_predictive_intent(natural_language)
    if not is_predictive:
        return None

    log.info(f"Predictive query detected. customer_filter={customer_filter}")

    try:
        conn = await connection_service.get_connection(db, current, connection_id)
    except Exception:
        return None

    # Step 2: Fetch invoice history (multi-source)
    invoice_rows = await _fetch_invoice_history(conn, customer_filter)
    if not invoice_rows:
        return ChatResponse(
            type="conversational",
            message="I couldn't find any invoice history in the database to generate predictions. Please ensure the ArInvoice table has data.",
        )

    # Step 3: Fetch customer names from ArCustomer
    customer_names = await _fetch_customer_names(conn)

    # Step 4: Fetch outstanding balances from ArCustomerBal
    balances = await _fetch_balances(conn)

    # Step 5: Calculate order patterns in Python
    predictions = _calculate_predictions(invoice_rows, customer_names, balances)

    if not predictions:
        return ChatResponse(
            type="conversational",
            message="Not enough invoice history (minimum 2 invoices per customer) to generate order predictions.",
        )

    # Step 6: Format for LLM
    prediction_text = _format_predictions_for_llm(predictions)

    # Step 7: LLM generates reasoned narrative
    try:
        ai_response = await get_llm().chat_text(
            system=PREDICTIVE_EXPLAINER_PROMPT.format(customer_predictions=prediction_text),
            user="Generate the customer order predictions now.",
        )
    except Exception as e:
        log.warning(f"predictive_llm_failed: {e}")
        ai_response = prediction_text

    # Step 8: Build display table
    display_rows = [
        {
            "Customer": p["customer_code"],
            "Name": p["customer_name"],
            "Last Invoice (days ago)": p["days_since_last"],
            "Avg Order Cycle (days)": round(p["avg_cycle_days"], 1),
            "Predicted Next Order": p["predicted_next_order"],
            "Confidence": p["confidence_label"],
        }
        for p in predictions[:15]  # top 15
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
        columns=["Customer", "Name", "Last Invoice (days ago)", "Avg Order Cycle (days)", "Predicted Next Order", "Confidence"],
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
            r.get("Customer") or r.get("customer"): float(r.get("Balance") or r.get("balance") or 0)
            for r in (res.rows or [])
        }
    except Exception:
        return {}


# ── Python Math Engine ────────────────────────────────────────────────────────

def _calculate_predictions(
    rows: List[Dict],
    customer_names: Dict[str, str],
    balances: Dict[str, float],
) -> List[Dict]:
    """Core RFM + order cycle calculation — pure Python, zero hallucination."""
    # Group invoices by customer
    customer_invoices: Dict[str, List[date]] = {}
    for r in rows:
        cust = r.get("Customer") or r.get("customer")
        raw_date = r.get("InvoiceDate") or r.get("invoicedate")
        if not cust or not raw_date:
            continue
        inv_date = _parse_date(raw_date)
        if inv_date:
            customer_invoices.setdefault(cust, []).append(inv_date)

    # Use max invoice date from dataset as 'today' to keep calculations accurate for historical data
    all_dates = [d for dates in customer_invoices.values() for d in dates]
    today = max(all_dates) if all_dates else date.today()
    predictions = []

    for cust, dates in customer_invoices.items():
        dates_sorted = sorted(set(dates))  # unique invoice dates

        # Need at least 2 data points for cycle calculation
        if len(dates_sorted) < 2:
            continue

        # Recency
        last_invoice_date = dates_sorted[-1]
        days_since_last = (today - last_invoice_date).days

        # Frequency & Cycle
        total_invoices = len(dates_sorted)
        gaps = [(dates_sorted[i+1] - dates_sorted[i]).days for i in range(len(dates_sorted)-1)]
        avg_cycle = mean(gaps)
        cycle_std = stdev(gaps) if len(gaps) > 1 else 0.0

        # Consistency score (lower std relative to mean = more consistent)
        if avg_cycle > 0:
            consistency_pct = max(0, round((1 - (cycle_std / avg_cycle)) * 100, 1))
        else:
            consistency_pct = 0.0

        # Predicted next order
        days_until_next = max(0, round(avg_cycle - days_since_last))
        predicted_date = today + timedelta(days=days_until_next)

        # Confidence label
        if consistency_pct >= 70:
            confidence = "High"
        elif consistency_pct >= 40:
            confidence = "Medium"
        else:
            confidence = "Low"

        # Churn signal
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

    # Sort: soonest predicted order first, then by confidence
    predictions.sort(key=lambda x: (x["days_until_next"], -x["consistency_pct"]))
    return predictions


def _format_predictions_for_llm(predictions: List[Dict]) -> str:
    """Format calculated metrics as structured text for LLM synthesis."""
    lines = ["CUSTOMER ORDER PREDICTIONS (based on real invoice data):\n"]
    for i, p in enumerate(predictions[:10], 1):
        churn_note = " ⚠️ CHURN RISK (overdue by cycle)" if p["is_churn_risk"] else ""
        lines.append(
            f"{i}. Customer: {p['customer_code']} ({p['customer_name']}){churn_note}\n"
            f"   - Last Invoice: {p['last_invoice_date']} ({p['days_since_last']} days ago)\n"
            f"   - Total Invoices in 12mo: {p['total_invoices']}\n"
            f"   - Avg Order Cycle: {p['avg_cycle_days']:.1f} days "
            f"(±{p['cycle_std_days']} days std dev)\n"
            f"   - Consistency Score: {p['consistency_pct']}%\n"
            f"   - Days Until Predicted Next Order: {p['days_until_next']} days "
            f"({p['predicted_next_order']})\n"
            f"   - Outstanding Balance: ${p['outstanding_balance']:,.2f}\n"
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
                return datetime.strptime(raw[:10], fmt[:8] if len(fmt) > 8 else fmt).date()
            except ValueError:
                continue
    return None
