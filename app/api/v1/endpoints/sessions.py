from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

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
    db: AsyncSession = Depends(get_db),
) -> list[SessionRead]:
    return await session_service.list_sessions(db, current)


@router.post("", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
async def create(
    data: SessionCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> SessionRead:
    return await session_service.create(db, current, data)


@router.get("/{session_id}", response_model=SessionDetail)
async def get(
    session_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> SessionDetail:
    return await session_service.get_detail(db, current, session_id)


@router.patch("/{session_id}", response_model=SessionRead)
async def update(
    session_id: uuid.UUID,
    data: SessionUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> SessionRead:
    return await session_service.update(db, current, session_id, data)


@router.post("/{session_id}/archive", response_model=SessionRead)
async def archive(
    session_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> SessionRead:
    return await session_service.archive(db, current, session_id)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete(
    session_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await session_service.delete(db, current, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
