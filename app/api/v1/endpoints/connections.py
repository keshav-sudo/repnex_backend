from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.rate_limit import rate_limit
from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.connection import (
    AccessGrantRead,
    AccessGrantRequest,
    ConnectionCreate,
    ConnectionRead,
    ConnectionUpdate,
    TestConnectionResponse,
)
from app.services import connection_service

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("", response_model=list[ConnectionRead])
async def list_(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> list[ConnectionRead]:
    return await connection_service.list_connections(db, current)


@router.post("", response_model=ConnectionRead, status_code=status.HTTP_201_CREATED)
async def create(
    data: ConnectionCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> ConnectionRead:
    return await connection_service.create_connection(db, current, data)


@router.get("/{conn_id}", response_model=ConnectionRead)
async def get(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> ConnectionRead:
    conn = await connection_service.get_connection(db, current, conn_id)
    return ConnectionRead.model_validate(conn)


@router.patch("/{conn_id}", response_model=ConnectionRead)
async def update(
    conn_id: uuid.UUID,
    data: ConnectionUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> ConnectionRead:
    return await connection_service.update_connection(db, current, conn_id, data)


@router.delete("/{conn_id}", status_code=status.HTTP_200_OK)
async def delete(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await connection_service.delete_connection(db, current, conn_id)
    return {"ok": True}


@router.post("/{conn_id}/test", response_model=TestConnectionResponse)
async def test(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> TestConnectionResponse:
    return await connection_service.test_connection(db, current, conn_id)


@router.post("/{conn_id}/access", response_model=AccessGrantRead, status_code=status.HTTP_201_CREATED)
async def grant_access(
    conn_id: uuid.UUID,
    data: AccessGrantRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> AccessGrantRead:
    return await connection_service.grant_access(db, current, conn_id, data)


@router.delete("/access/{grant_id}", status_code=status.HTTP_200_OK)
async def revoke_access(
    grant_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await connection_service.revoke_access(db, current, grant_id)
    return {"ok": True}
