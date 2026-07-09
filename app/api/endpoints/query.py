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


# ── Routes ────────────────────────────────────────────────────────────────────


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
    return {"status": "success", "message": "Feedback saved successfully"}
