from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.rate_limit import rate_limit
from app.api.v1.dependencies.tenancy import bind_tenant_context
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


@router.post("/chat", response_model=ChatResponse)
async def chat(
    data: ChatRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("query")),
) -> ChatResponse:
    """Unified chat endpoint: classifies intent and returns appropriate response."""
    return await query_service.chat(db, current, data=data)


@router.post("/execute", response_model=ChatResponse)
async def execute_with_params(
    data: ExecuteRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("query")),
) -> ChatResponse:
    """Execute a template with explicit parameters (after params_needed)."""
    return await query_service.execute_with_params(db, current, data=data)


@router.post("/run", response_model=RunQueryResponse)
async def run_query(
    data: RunQueryRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("query")),
) -> RunQueryResponse:
    """Legacy endpoint: run query in one shot."""
    return await query_service.run_via_rest(
        db,
        current,
        session_id=data.session_id,
        natural_language=data.natural_language,
    )


@router.get("/templates")
async def list_templates(
    _: CurrentUser = Depends(bind_tenant_context),
) -> list[dict]:
    return get_template_registry().list_for_llm()
