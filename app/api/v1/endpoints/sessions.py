from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.session import (
    SessionCreate,
    SessionDetail,
    SessionRead,
    SessionUpdate,
)
from app.services import session_service

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=list[SessionRead])
async def list_(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[SessionRead]:
    return await session_service.list_sessions(db, current)


@router.post("", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
async def create(
    data: SessionCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> SessionRead:
    return await session_service.create(db, current, data)


@router.get("/{session_id}", response_model=SessionDetail)
async def get(
    session_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> SessionDetail:
    res = await session_service.get_detail(db, current, session_id)
    from app.api.v1.endpoints.query import is_sql_hidden, redact_sql_blocks
    if await is_sql_hidden(db, current.org_id):
        for turn in res.context_window:
            if "sql" in turn:
                turn["sql"] = None
            if "content" in turn and isinstance(turn["content"], str):
                cleaned = turn["content"]
                cleaned = cleaned.replace(" Here's a preview of the SQL that will execute:", "")
                cleaned = cleaned.replace("Here's a preview of the SQL that will execute:", "")
                turn["content"] = redact_sql_blocks(cleaned)
    return res


@router.patch("/{session_id}", response_model=SessionRead)
async def update(
    session_id: uuid.UUID,
    data: SessionUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> SessionRead:
    return await session_service.update(db, current, session_id, data)


@router.post("/{session_id}/archive", response_model=SessionRead)
async def archive(
    session_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> SessionRead:
    return await session_service.archive(db, current, session_id)


@router.delete("/{session_id}", status_code=status.HTTP_200_OK)
async def delete(
    session_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    await session_service.delete(db, current, session_id)
    return {"ok": True}
