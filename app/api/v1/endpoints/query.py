import re
import uuid
from typing import Any
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.rate_limit import rate_limit
from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.models import Organization
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.query_engine.template_loader import get_template_registry
from app.schemas.query import (
    ChatRequest,
    ChatResponse,
    ExecuteRequest,
    RunQueryRequest,
    RunQueryResponse,
)
from app.services import query_service

router = APIRouter(prefix="/query", tags=["query"])


async def is_sql_hidden(db: AsyncSession, org_id: uuid.UUID) -> bool:
    stmt = select(Organization.hide_sql_queries).where(Organization.id == org_id)
    res = await db.execute(stmt)
    return bool(res.scalar())


def redact_sql_blocks(text: str | None) -> str | None:
    if not text:
        return text
    return re.sub(
        r"(```sql\s+)(.*?)(```)",
        r"\1-- SQL hidden by organization settings\n\3",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )


async def apply_sql_redaction(db: AsyncSession, org_id: uuid.UUID, res: Any) -> Any:
    if await is_sql_hidden(db, org_id):
        if hasattr(res, "sql") and res.sql:
            res.sql = "-- SQL hidden by organization settings"
        if hasattr(res, "message") and res.message:
            res.message = redact_sql_blocks(res.message)
        if hasattr(res, "summary") and res.summary:
            res.summary = redact_sql_blocks(res.summary)
    return res


@router.post("/chat", response_model=ChatResponse)
async def chat(
    data: ChatRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    # _rl: None = Depends(rate_limit("query")),
) -> ChatResponse:
    """Unified chat endpoint: classifies intent and returns appropriate response."""
    res = await query_service.chat(db, current, data=data)
    return await apply_sql_redaction(db, current.org_id, res)


@router.post("/execute", response_model=ChatResponse)
async def execute_with_params(
    data: ExecuteRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    # _rl: None = Depends(rate_limit("query")),
) -> ChatResponse:
    """Execute a template with explicit parameters (after params_needed)."""
    res = await query_service.execute_with_params(db, current, data=data)
    return await apply_sql_redaction(db, current.org_id, res)


@router.post("/run", response_model=RunQueryResponse)
async def run_query(
    data: RunQueryRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    # _rl: None = Depends(rate_limit("query")),
) -> RunQueryResponse:
    """Legacy endpoint: run query in one shot."""
    res = await query_service.run_via_rest(
        db,
        current,
        session_id=data.session_id,
        natural_language=data.natural_language,
    )
    return await apply_sql_redaction(db, current.org_id, res)


@router.get("/templates")
async def list_templates(
    _: CurrentUser = Depends(bind_tenant_context),
) -> list[dict]:
    return get_template_registry().list_for_llm()

