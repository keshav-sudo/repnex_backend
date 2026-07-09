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


async def _is_sql_hidden(db: AsyncIOMotorDatabase, org_id: uuid.UUID) -> bool:
    org = await db[Organization.COLLECTION].find_one({"_id": str(org_id)})
    return bool(org.get("hide_sql_queries") if org else False)


def _redact_sql_blocks(text: str | None) -> str | None:
    if not text:
        return text
    return re.sub(
        r"(```sql\s+)(.*?)(```)",
        r"\1-- SQL hidden by organisation settings\n\3",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )


async def _apply_sql_redaction(db: AsyncIOMotorDatabase, org_id: uuid.UUID, res: Any) -> Any:
    if await _is_sql_hidden(db, org_id):
        if hasattr(res, "sql"):
            res.sql = None
        if hasattr(res, "message") and res.message:
            cleaned = res.message.replace(
                " Here's a preview of the SQL that will execute:", ""
            ).replace("Here's a preview of the SQL that will execute:", "")
            res.message = _redact_sql_blocks(cleaned)
        if hasattr(res, "summary") and res.summary:
            res.summary = _redact_sql_blocks(res.summary)
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
    return await _apply_sql_redaction(db, current.org_id, res)


@router.post("/execute", response_model=ChatResponse)
async def execute_endpoint(
    data: ExecuteRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ChatResponse:
    """Execute a V2 query with user-supplied date parameters (after params_needed)."""
    res = await execute_with_params(db, current, data=data)
    return await _apply_sql_redaction(db, current.org_id, res)


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
    return await _apply_sql_redaction(db, current.org_id, res)
