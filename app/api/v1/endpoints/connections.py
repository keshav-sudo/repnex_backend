from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
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
    ListDatabasesRequest,
    ListDatabasesResponse,
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


# ── Static sub-routes MUST come before /{conn_id} dynamic routes ─────────────
# If /test or /list-databases are placed after /{conn_id}, FastAPI will treat
# the string literal "test" / "list-databases" as a UUID → 422 Unprocessable.

@router.post("/test", response_model=TestConnectionResponse)
async def test_raw(
    data: ConnectionCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    _rl: None = Depends(rate_limit("api")),
) -> TestConnectionResponse:
    """Test credentials without saving — used by the 'Test Connection' button."""
    return await connection_service.test_raw_connection(current, data)


@router.post("/list-databases", response_model=ListDatabasesResponse)
async def list_databases(
    data: ListDatabasesRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    _rl: None = Depends(rate_limit("api")),
) -> ListDatabasesResponse:
    """
    Connect to the server with provided credentials and return available databases.
    Used to populate the database dropdown before the user picks one.
    """
    try:
        return await connection_service.list_databases(current, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/gateway-agents", response_model=list[str])
async def list_gateway_agents(
    current: CurrentUser = Depends(bind_tenant_context),
) -> list[str]:
    """Return names of gateway agents currently connected for this org."""
    from app.services.gateway_manager import get_gateway_manager
    try:
        mgr = get_gateway_manager()
        return mgr.list_active_agents(current.org_id)
    except Exception:
        return []


# ── Dynamic /{conn_id} routes ─────────────────────────────────────────────────

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
