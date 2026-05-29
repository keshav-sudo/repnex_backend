from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.dashboard import (
    DashboardCreate,
    DashboardItemAdd,
    DashboardItemUpdate,
    DashboardRead,
    DashboardUpdate,
)
from app.services import dashboard_service

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


@router.get("", response_model=list[DashboardRead])
async def list_(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> list[DashboardRead]:
    return await dashboard_service.list_dashboards(db, current)


@router.post("", response_model=DashboardRead, status_code=status.HTTP_201_CREATED)
async def create(
    data: DashboardCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> DashboardRead:
    return await dashboard_service.create(db, current, data)


@router.get("/{dashboard_id}", response_model=DashboardRead)
async def get(
    dashboard_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> DashboardRead:
    d = await dashboard_service.get_dashboard(db, current, dashboard_id)
    return DashboardRead.model_validate(d)


@router.patch("/{dashboard_id}", response_model=DashboardRead)
async def update(
    dashboard_id: uuid.UUID,
    data: DashboardUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> DashboardRead:
    return await dashboard_service.update(db, current, dashboard_id, data)


@router.delete("/{dashboard_id}", status_code=status.HTTP_200_OK)
async def delete(
    dashboard_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await dashboard_service.delete(db, current, dashboard_id)
    return {"ok": True}


@router.post(
    "/{dashboard_id}/items", response_model=DashboardRead, status_code=status.HTTP_201_CREATED
)
async def add_item(
    dashboard_id: uuid.UUID,
    data: DashboardItemAdd,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> DashboardRead:
    return await dashboard_service.add_item(db, current, dashboard_id, data)


@router.patch("/{dashboard_id}/items/{item_id}", response_model=DashboardRead)
async def update_item(
    dashboard_id: uuid.UUID,
    item_id: uuid.UUID,
    data: DashboardItemUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> DashboardRead:
    return await dashboard_service.update_item(db, current, dashboard_id, item_id, data)


@router.delete("/{dashboard_id}/items/{item_id}", status_code=status.HTTP_200_OK)
async def remove_item(
    dashboard_id: uuid.UUID,
    item_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await dashboard_service.remove_item(db, current, dashboard_id, item_id)
    return {"ok": True}
