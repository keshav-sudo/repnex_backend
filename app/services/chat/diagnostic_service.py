import json
import logging
import uuid
import re
from datetime import date, timedelta
from typing import Any, Tuple, Dict, List
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.exceptions import LLMError, TargetDBError
from app.llm.client import get_llm
from app.engine import BoundQuery, execute_collect
from app.services import connection_service
from app.schemas.query import ChatResponse, IntentResult

log = logging.getLogger(__name__)

# System prompt to identify if a query is a customer margin diagnostic query
DIAGNOSTIC_CLASSIFIER_PROMPT = """\
You are an expert ERP intent classifier. Your job is to determine if the user's natural language question is asking "why" a specific customer's profit margin or profitability dropped, decreased, or changed.

If it is, extract the customer name or customer code.
If dates/periods are mentioned (e.g. "in April", "last month", "in Q2"), extract them as a description.

Return a JSON object:
{
  "is_diagnostic": true | false,
  "customer_name": "<extracted customer name or code, or null>",
  "period_description": "<extracted period info, or null>"
}

Output ONLY the JSON object. No markdown, no explanation.
"""

# System prompt to generate final explanation using calculated variances
DIAGNOSTIC_EXPLAINER_PROMPT = """\
You are a precise data reporting assistant. You are given a variance analysis report explaining why a customer's profit margin dropped between a Peak Period (high margin) and a Drop Period (low margin).
Your job is to explain these findings in plain, simple business terms.

Inputs:
- Customer Code/Name: {customer_name}
- Peak Period: {peak_period} (Margin: {peak_margin_pct:.1f}%)
- Drop Period: {drop_period} (Margin: {drop_margin_pct:.1f}%)
- Total Margin Change: ${total_margin_variance:,.2f}
- Price Impact: ${price_impact:,.2f} (Negative means lower average selling prices or discounting reduced margin)
- Cost Impact: ${cost_impact:,.2f} (Negative means increased product costs from suppliers reduced margin)
- Mix & Volume Impact: ${mix_volume_impact:,.2f} (Negative means shifts to lower-margin products or lower volume reduced margin)

Top Product Drivers of the Change:
{product_drivers}

Guidelines:
1. Start with a direct, professional summary: "The profit margin for {customer_name} fell from {peak_margin_pct:.1f}% in {peak_period} to {drop_margin_pct:.1f}% in {drop_period}, resulting in a total margin change of ${total_margin_variance:,.2f}."
2. Explain the main driver of the drop (Price vs Cost vs Product Mix shift) using the calculated impacts.
3. List the top 2-3 specific products that caused the drop.
4. Keep the explanation factual, helpful, and concise. Do not invent any facts, numbers, or reasons not supported by the math above.
"""

async def detect_and_run_diagnostic(
    db: AsyncIOMotorDatabase,
    current: Any,
    connection_id: uuid.UUID | None,
    natural_language: str
) -> ChatResponse | None:
    """Detects if query is a customer margin diagnostic query and executes it if so."""
    if not connection_id:
        return None

    # Step 1: Run intent classifier
    try:
        raw_res = await get_llm().chat_json(
            system=DIAGNOSTIC_CLASSIFIER_PROMPT,
            user=f"Question: {natural_language}"
        )
        is_diag = raw_res.get("is_diagnostic", False)
        cust_name = raw_res.get("customer_name")
        period_desc = raw_res.get("period_description")
    except Exception as e:
        log.warning(f"Failed to run diagnostic classifier: {e}")
        return None

    if not is_diag or not cust_name:
        return None

    log.info(f"Diagnostic query detected for customer: {cust_name}")

    try:
        conn = await connection_service.get_connection(db, current, connection_id)
    except Exception:
        return None

    # Step 2: Look up the customer in the database to get exact code
    # We query ArCustomer or SorMaster to find matching Customer code
    customer_code = await _resolve_customer_code(conn, cust_name)
    if not customer_code:
        return ChatResponse(
            type="conversational",
            message=f"I identified that you want to analyze profit margins for '{cust_name}', but I couldn't find a customer matching that name in your database. Please verify the customer name."
        )

    # Step 3: Fetch order/invoice history for the last 12 months to analyze trends
    start_date_limit = (date.today() - timedelta(days=365)).isoformat()
    
    # Query to fetch raw sales lines for this customer
    # Works across MySQL, Postgres, and SQL Server since we do not use dialect-specific date formatting
    history_sql = """
    SELECT 
        sm.OrderDate,
        sd.ProductCode,
        sd.MShipQty,
        sd.MPrice,
        sd.MUnitCost
    FROM SorDetail sd
    INNER JOIN SorMaster sm ON sd.SalesOrder = sm.SalesOrder
    WHERE sm.Customer = %s
      AND sm.OrderDate >= %s
      AND sd.MShipQty > 0
    """
    
    # Adapt placeholder syntax to driver
    if conn.db_type.value in ("postgres", "cloudsql"):
        adapted_sql = history_sql.replace("%s", "$1", 1).replace("%s", "$2", 1)
        params = [customer_code, start_date_limit]
    else:
        adapted_sql = history_sql
        params = (customer_code, start_date_limit)
        
    bound = BoundQuery(sql=adapted_sql, params={}, db_type=conn.db_type.value)
    
    try:
        result = await execute_collect(conn, bound, params=params)
    except TargetDBError as e:
        log.error(f"Failed to fetch order history for diagnostic: {e}")
        return ChatResponse(
            type="error",
            message=f"Could not execute diagnostic analysis due to target database error: {e.message}"
        )

    if not result.rows:
        return ChatResponse(
            type="conversational",
            message=f"I found the customer '{customer_code}', but they don't have any sales order records in the last 12 months to analyze."
        )

    # Step 4: Aggregate data by month in Python (100% dialect agnostic)
    monthly_data = _group_by_month(result.rows)
    if len(monthly_data) < 2:
        return ChatResponse(
            type="conversational",
            message=f"Customer '{customer_code}' only has sales in a single month. Diagnostic margin variance analysis requires at least two months of data to compare."
        )

    # Step 5: Identify Peak (Good) vs Drop (Bad) periods
    peak_period, drop_period = _identify_periods(monthly_data, period_desc)
    
    # Step 6: Calculate product-level variances
    variance_results = _run_variance_analysis(
        monthly_data[peak_period]["products"], 
        monthly_data[drop_period]["products"]
    )
    
    # Step 7: Format top drivers for LLM
    product_drivers = _format_product_drivers(variance_results["product_details"])
    
    # Step 8: Generate synthesis using LLM
    summary_prompt = DIAGNOSTIC_EXPLAINER_PROMPT.format(
        customer_name=customer_code,
        peak_period=peak_period,
        peak_margin_pct=monthly_data[peak_period]["margin_pct"],
        drop_period=drop_period,
        drop_margin_pct=monthly_data[drop_period]["margin_pct"],
        total_margin_variance=variance_results["summary"]["total_margin_variance"],
        price_impact=variance_results["summary"]["price_impact"],
        cost_impact=variance_results["summary"]["cost_impact"],
        mix_volume_impact=variance_results["summary"]["mix_volume_impact"],
        product_drivers=product_drivers
    )
    
    try:
        ai_response = await get_llm().chat_text(system=summary_prompt, user="Generate summary now.")
    except Exception as e:
        ai_response = f"Diagnostic analysis completed. Total margin change is ${variance_results['summary']['total_margin_variance']:,.2f} (Price impact: ${variance_results['summary']['price_impact']:,.2f}, Cost impact: ${variance_results['summary']['cost_impact']:,.2f}, Mix/Volume impact: ${variance_results['summary']['mix_volume_impact']:,.2f})."

    # Format result table columns and rows for UI display
    display_rows = [
        {
            "Category": "Total Margin Change",
            "Impact ($)": variance_results["summary"]["total_margin_variance"]
        },
        {
            "Category": "Price Impact (Discounts)",
            "Impact ($)": variance_results["summary"]["price_impact"]
        },
        {
            "Category": "Cost Impact (Supplier pricing)",
            "Impact ($)": variance_results["summary"]["cost_impact"]
        },
        {
            "Category": "Mix & Volume Impact",
            "Impact ($)": variance_results["summary"]["mix_volume_impact"]
        }
    ]

    intent = IntentResult(
        template_id="diagnostic_variance_analysis",
        params={"customer_code": customer_code, "peak_period": peak_period, "drop_period": drop_period},
        confidence=1.0,
        rationale="agentic_variance_analysis"
    )

    return ChatResponse(
        type="executable",
        message=ai_response,
        template_id="diagnostic_variance_analysis",
        template_description=f"Margin Diagnostics - Customer {customer_code}",
        template_module="diagnostic_engine",
        sql=f"-- Variance analysis run for {customer_code} comparing {peak_period} vs {drop_period}\n{adapted_sql}",
        rows=display_rows,
        columns=["Category", "Impact ($)"],
        rows_returned=len(display_rows),
        execution_time_ms=result.execution_time_ms,
        summary=ai_response,
        suggestions=["Why did overall company margin drop?", f"Top sold products to {customer_code}"],
        intent=intent
    )

async def _resolve_customer_code(conn: Any, name_query: str) -> str | None:
    """Helper to resolve a customer name or partial code to the exact customer code."""
    # Query ArCustomer (standard Syspro customer master)
    sql = "SELECT Customer FROM ArCustomer WHERE Customer = %s OR CustomerName LIKE %s"
    
    # Try exact match first, then partial match
    like_query = f"%{name_query}%"
    
    if conn.db_type.value in ("postgres", "cloudsql"):
        adapted_sql = sql.replace("%s", "$1", 1).replace("%s", "$2", 1)
        params = [name_query, like_query]
    else:
        adapted_sql = sql
        params = (name_query, like_query)
        
    bound = BoundQuery(sql=adapted_sql, params={}, db_type=conn.db_type.value)
        
    try:
        res = await execute_collect(conn, bound, params=params)
        if res.rows:
            # Return first matched customer code
            return res.rows[0].get("Customer") or res.rows[0].get("customer")
    except Exception:
        pass
        
    # Fallback to SorMaster if ArCustomer table is not present
    sql_alt = "SELECT DISTINCT Customer FROM SorMaster WHERE Customer = %s OR CustomerName LIKE %s"
    if conn.db_type.value in ("postgres", "cloudsql"):
        adapted_sql_alt = sql_alt.replace("%s", "$1", 1).replace("%s", "$2", 1)
    else:
        adapted_sql_alt = sql_alt
        
    bound_alt = BoundQuery(sql=adapted_sql_alt, params={}, db_type=conn.db_type.value)
    try:
        res = await execute_collect(conn, bound_alt, params=params)
        if res.rows:
            return res.rows[0].get("Customer") or res.rows[0].get("customer")
    except Exception:
        pass
        
    # If the user inputted what looks like a code, return it as a fallback
    if len(name_query) <= 15 and name_query.isalnum():
        return name_query.upper()
        
    return None

def _group_by_month(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Groups order lines by month and aggregates totals."""
    months = {}
    for r in rows:
        # Normalize keys regardless of case
        o_date = r.get("OrderDate") or r.get("orderdate")
        stock_code = r.get("ProductCode") or r.get("productcode")
        qty = float(r.get("MShipQty") or r.get("mshipqty") or 0.0)
        price = float(r.get("MPrice") or r.get("mprice") or 0.0)
        cost = float(r.get("MUnitCost") or r.get("munitcost") or 0.0)
        
        if not o_date:
            continue
            
        # Parse year-month
        if isinstance(o_date, str):
            # E.g. '2026-05-15' -> '2026-05'
            match = re.match(r"(\d{4}-\d{2})", o_date)
            ym = match.group(1) if match else "Unknown"
        else:
            # datetime object
            ym = o_date.strftime("%Y-%m")
            
        if ym not in months:
            months[ym] = {
                "sales": 0.0,
                "cost": 0.0,
                "margin": 0.0,
                "products": {}
            }
            
        sales_val = qty * price
        cost_val = qty * cost
        
        months[ym]["sales"] += sales_val
        months[ym]["cost"] += cost_val
        
        # Product level aggregation
        p_map = months[ym]["products"]
        if stock_code not in p_map:
            p_map[stock_code] = {"qty": 0.0, "sales": 0.0, "cost": 0.0}
        p_map[stock_code]["qty"] += qty
        p_map[stock_code]["sales"] += sales_val
        p_map[stock_code]["cost"] += cost_val

    # Post-process percentages
    for ym, data in list(months.items()):
        if data["sales"] > 0:
            data["margin"] = data["sales"] - data["cost"]
            data["margin_pct"] = (data["margin"] / data["sales"]) * 100.0
        else:
            del months[ym]
            
    return months

def _identify_periods(months: Dict[str, Dict[str, Any]], period_desc: str | None) -> Tuple[str, str]:
    """Identifies the peak period and the drop period from the monthly data."""
    sorted_months = sorted(months.keys())
    
    # Default selection:
    # Drop period is the latest month, Peak period is the month with highest margin pct before it
    drop_period = sorted_months[-1]
    
    # Find specific drop period if mentioned in description (e.g. 'April')
    if period_desc:
        desc_lower = period_desc.lower()
        month_names = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
            "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
        }
        for name, num in month_names.items():
            if name in desc_lower:
                # Find year matching this month
                matched = [m for m in sorted_months if m.endswith(f"-{num}")]
                if matched:
                    drop_period = matched[-1]
                    break

    # Peak period is the month with the highest margin percentage in the history
    peak_period = sorted_months[0]
    highest_margin = -999999.0
    
    for ym in sorted_months:
        if ym == drop_period:
            continue
        m_pct = months[ym]["margin_pct"]
        if m_pct > highest_margin:
            highest_margin = m_pct
            peak_period = ym
            
    return peak_period, drop_period

def _run_variance_analysis(p1_products: Dict[str, Dict], p2_products: Dict[str, Dict]) -> Dict[str, Any]:
    """Calculates Price, Cost, and Mix variances between two periods."""
    all_products = set(p1_products.keys()) | set(p2_products.keys())
    
    results = []
    total_price_var = 0.0
    total_cost_var = 0.0
    total_margin_var = 0.0
    
    for prod in all_products:
        p1 = p1_products.get(prod, {"qty": 0.0, "sales": 0.0, "cost": 0.0})
        p2 = p2_products.get(prod, {"qty": 0.0, "sales": 0.0, "cost": 0.0})
        
        qty1, qty2 = p1["qty"], p2["qty"]
        price1 = p1["sales"] / qty1 if qty1 > 0 else 0.0
        price2 = p2["sales"] / qty2 if qty2 > 0 else 0.0
        cost1 = p1["cost"] / qty1 if qty1 > 0 else 0.0
        cost2 = p2["cost"] / qty2 if qty2 > 0 else 0.0
        
        margin1 = p1["sales"] - p1["cost"]
        margin2 = p2["sales"] - p2["cost"]
        
        # Math formulas:
        # Price Variance = (Price2 - Price1) * Qty2
        price_var = (price2 - price1) * qty2 if qty2 > 0 else 0.0
        # Cost Variance = (Cost1 - Cost2) * Qty2
        cost_var = (cost1 - cost2) * qty2 if qty2 > 0 else 0.0
        # Total Margin Change
        margin_var = margin2 - margin1
        # Mix/Volume (Residual)
        mix_vol_var = margin_var - (price_var + cost_var)
        
        results.append({
            "ProductCode": prod,
            "PriceVariance": price_var,
            "CostVariance": cost_var,
            "MixVolumeVariance": mix_vol_var,
            "TotalMarginVariance": margin_var
        })
        
        total_price_var += price_var
        total_cost_var += cost_var
        total_margin_var += margin_var
        
    total_mix_vol_var = total_margin_var - (total_price_var + total_cost_var)
    
    return {
        "summary": {
            "total_margin_variance": total_margin_var,
            "price_impact": total_price_var,
            "cost_impact": total_cost_var,
            "mix_volume_impact": total_mix_vol_var
        },
        "product_details": results
    }

def _format_product_drivers(products: List[Dict[str, Any]]) -> str:
    """Formats top products dragging margin down as markdown bullet points."""
    # Sort products by total margin variance (most negative first)
    sorted_prods = sorted(products, key=lambda x: x["TotalMarginVariance"])
    top_negative = [p for p in sorted_prods if p["TotalMarginVariance"] < 0][:3]
    
    if not top_negative:
        return "No specific products saw a margin decrease."
        
    bullets = []
    for p in top_negative:
        bullets.append(
            f"- **Product {p['ProductCode']}**: Total margin change of ${p['TotalMarginVariance']:,.2f} "
            f"(Price impact: ${p['PriceVariance']:,.2f}, Cost impact: ${p['CostVariance']:,.2f})"
        )
    return "\n".join(bullets)
