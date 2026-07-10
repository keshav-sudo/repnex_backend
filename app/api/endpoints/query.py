"""Query endpoints — V2 Semantic Engine only."""
from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies.tenancy import bind_tenant_context
from app.core.database.models import Organization
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.query import (
    ChatRequest,
    ChatResponse,
    ExecuteRequest,
    RunQueryRequest,
    RunQueryResponse,
)
from app.services.chat import chat, execute_with_params, run_via_rest

router = APIRouter(prefix="/query", tags=["query"])


# ── SQL redaction (org-level setting) ─────────────────────────────────────────


async def is_sql_hidden(db: AsyncIOMotorDatabase, org_id: uuid.UUID) -> bool:
    org = await db[Organization.COLLECTION].find_one({"_id": str(org_id)})
    return bool(org.get("hide_sql_queries") if org else False)


def redact_sql_blocks(text: str | None) -> str | None:
    if not text:
        return text
    return re.sub(
        r"(```sql\s+)(.*?)(```)",
        r"\1-- SQL hidden by organisation settings\n\3",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )


async def apply_sql_redaction(db: AsyncIOMotorDatabase, org_id: uuid.UUID, res: Any) -> Any:
    if await is_sql_hidden(db, org_id):
        if hasattr(res, "sql"):
            res.sql = None
        if hasattr(res, "message") and res.message:
            cleaned = res.message.replace(
                " Here's a preview of the SQL that will execute:", ""
            ).replace("Here's a preview of the SQL that will execute:", "")
            res.message = redact_sql_blocks(cleaned)
        if hasattr(res, "summary") and res.summary:
            res.summary = redact_sql_blocks(res.summary)
    return res


import json

DEFAULT_SUGGESTIONS = [
    {
        "category": "AP & Suppliers",
        "prompts": [
            { "text": "Show AP ageing report with 30-60-90 buckets", "icon": "📊" },
            { "text": "List overdue supplier invoices as of today", "icon": "⚠️" },
            { "text": "Top 10 suppliers by outstanding amount", "icon": "🏆" },
            { "text": "Supplier payment history last 3 months", "icon": "💳" },
        ],
    },
    {
        "category": "AR & Customers",
        "prompts": [
            { "text": "Customer ageing report with overdue buckets", "icon": "📋" },
            { "text": "Top 10 customers by outstanding receivables", "icon": "📈" },
            { "text": "Overdue customer invoices older than 60 days", "icon": "⚠️" },
            { "text": "Customer payment collection trend this quarter", "icon": "💰" },
        ],
    },
    {
        "category": "Cashbook & GL",
        "prompts": [
            { "text": "Cashbook summary for current month", "icon": "💵" },
            { "text": "GL journal entries posted today", "icon": "📝" },
            { "text": "Trial balance for current period", "icon": "📑" },
            { "text": "Bank reconciliation status report", "icon": "🏦" },
        ],
    },
    {
        "category": "Sales & Revenue",
        "prompts": [
            { "text": "Sales orders by customer this month", "icon": "🛒" },
            { "text": "Top 10 customers by revenue", "icon": "🏆" },
            { "text": "Monthly revenue trend last 6 months", "icon": "📈" },
            { "text": "Outstanding sales orders summary", "icon": "📦" },
        ],
    },
]

@router.get("/suggestions")
async def get_suggestions_endpoint(
    connection_id: uuid.UUID | None = None,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Retrieve dynamic query suggestions based on connected DB schema with AI token caching."""
    conn = None
    if connection_id:
        conn = await db["connections"].find_one({
            "_id": str(connection_id),
            "org_id": str(current.org_id)
        })
    else:
        # Fallback to organization's first database connection
        conn = await db["connections"].find_one({
            "org_id": str(current.org_id)
        })

    if conn and "suggested_queries" in conn and conn["suggested_queries"]:
        return conn["suggested_queries"]

    if not conn or not conn.get("schema_info"):
        return DEFAULT_SUGGESTIONS

    tables = conn.get("schema_info", {}).get("tables", [])
    table_names = [t.get("name") for t in tables if t.get("name")]
    if not table_names:
        return DEFAULT_SUGGESTIONS

    # Token minimization: use first 20 tables & clean prompt format
    system_prompt = (
        "You are an expert ERP reporting assistant. Given a connected database schema's table names, "
        "suggest natural language query prompts categorized into 3-4 relevant business modules (e.g. Sales, Finance, Inventory). "
        "Respond ONLY with a JSON array matching the structure: "
        "[{\"category\": \"Category Name\", \"prompts\": [{\"text\": \"Prompt query text matching the table names\", \"icon\": \"Emoji icon\"}]}]"
    )
    user_payload = json.dumps({
        "tables": table_names[:20],
        "organization": str(current.org_id)
    })

    try:
        from app.llm.client import get_llm
        raw = await get_llm().chat_json(system=system_prompt, user=user_payload)
        
        suggestions = []
        if isinstance(raw, list):
            suggestions = raw
        elif isinstance(raw, dict) and "suggestions" in raw:
            suggestions = raw["suggestions"]
        elif isinstance(raw, dict):
            # Sometimes LLM wraps inside key named after categories or array
            for key, val in raw.items():
                if isinstance(val, list):
                    suggestions = val
                    break

        if suggestions:
            # Cache suggestions to minimize AI tokens on future requests
            await db["connections"].update_one(
                {"_id": str(conn["_id"])},
                {"$set": {"suggested_queries": suggestions}}
            )
            return suggestions

        return DEFAULT_SUGGESTIONS
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to generate dynamic suggestions: {e}")
        return DEFAULT_SUGGESTIONS


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    data: ChatRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    # _rl: None = Depends(rate_limit("query")),
) -> ChatResponse:
    """Classify intent and return conversational or executable response."""
    res = await chat(db, current, data=data)
    return await apply_sql_redaction(db, current.org_id, res)


@router.post("/execute", response_model=ChatResponse)
async def execute_endpoint(
    data: ExecuteRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ChatResponse:
    """Execute a V2 query with user-supplied date parameters (after params_needed)."""
    res = await execute_with_params(db, current, data=data)
    return await apply_sql_redaction(db, current.org_id, res)


@router.post("/run", response_model=RunQueryResponse)
async def run_endpoint(
    data: RunQueryRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> RunQueryResponse:
    """Legacy REST path — single-shot NL→SQL→execute."""
    res = await run_via_rest(
        db, current,
        session_id=data.session_id,
        natural_language=data.natural_language,
    )
    return await apply_sql_redaction(db, current.org_id, res)


from pydantic import BaseModel
from datetime import datetime, timezone

class FeedbackRequest(BaseModel):
    is_positive: bool
    category: str | None = None
    comment: str | None = None

@router.post("/history/{history_id}/feedback")
async def save_feedback(
    history_id: uuid.UUID,
    data: FeedbackRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Save user feedback (thumbs up / thumbs down with categories/comments) for a query history record."""
    from app.core.database.models import QueryHistory
    from fastapi import HTTPException

    history_doc = await db[QueryHistory.COLLECTION].find_one({
        "_id": str(history_id),
        "org_id": str(current.org_id)
    })

    if not history_doc:
        raise HTTPException(status_code=404, detail="Query history record not found")

    await db[QueryHistory.COLLECTION].update_one(
        {"_id": str(history_id)},
        {"$set": {
            "feedback": {
                "is_positive": data.is_positive,
                "category": data.category,
                "comment": data.comment,
                "submitted_at": datetime.now(timezone.utc)
            }
        }}
    )

    # Also save to a separate, dedicated "query_feedbacks" collection for separate admin view/exports
    feedback_doc = {
        "_id": str(uuid.uuid4()),
        "history_id": str(history_id),
        "org_id": str(current.org_id),
        "user_id": str(current.user_id),
        "is_positive": data.is_positive,
        "category": data.category,
        "comment": data.comment,
        "submitted_at": datetime.now(timezone.utc),
        # Snapshot key query info for easy admin dashboard viewing
        "natural_language_input": history_doc.get("natural_language_input"),
        "generated_sql": history_doc.get("generated_sql"),
    }
    await db["query_feedbacks"].insert_one(feedback_doc)

    return {"status": "success", "message": "Feedback saved successfully"}
