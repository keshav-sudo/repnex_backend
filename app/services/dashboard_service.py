from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.models import Dashboard, DashboardReport, Report
from app.core.exceptions import Conflict, Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.schemas.dashboard import (
    DashboardCreate,
    DashboardItemAdd,
    DashboardItemUpdate,
    DashboardRead,
    DashboardUpdate,
)


async def list_dashboards(
    db: AsyncSession, current: CurrentUser
) -> list[DashboardRead]:
    rows = (
        await db.execute(
            select(Dashboard)
            .where(Dashboard.org_id == current.org_id)
            .order_by(Dashboard.created_at.desc())
        )
    ).scalars().all()
    return [DashboardRead.model_validate(d) for d in rows]


async def get_dashboard(
    db: AsyncSession, current: CurrentUser, dashboard_id: uuid.UUID
) -> Dashboard:
    d = (
        await db.execute(
            select(Dashboard).where(
                Dashboard.id == dashboard_id, Dashboard.org_id == current.org_id
            )
        )
    ).scalar_one_or_none()
    if not d:
        raise NotFound("Dashboard not found")
    return d


async def create(
    db: AsyncSession, current: CurrentUser, data: DashboardCreate
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create dashboards")
    d = Dashboard(
        org_id=current.org_id,
        created_by=current.user_id,
        name=data.name,
        is_default=data.is_default,
        layout_config=data.layout_config,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return DashboardRead.model_validate(d)


async def update(
    db: AsyncSession,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    data: DashboardUpdate,
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot update dashboards")
    d = await get_dashboard(db, current, dashboard_id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(d, k, v)
    await db.commit()
    await db.refresh(d)
    return DashboardRead.model_validate(d)


async def delete(
    db: AsyncSession, current: CurrentUser, dashboard_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete dashboards")
    d = await get_dashboard(db, current, dashboard_id)
    await db.delete(d)
    await db.commit()


async def add_item(
    db: AsyncSession,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    data: DashboardItemAdd,
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot edit dashboards")
    d = await get_dashboard(db, current, dashboard_id)
    report = (
        await db.execute(
            select(Report).where(Report.id == data.report_id, Report.org_id == current.org_id)
        )
    ).scalar_one_or_none()
    if not report:
        raise NotFound("Report not found")
    item = DashboardReport(
        dashboard_id=d.id,
        report_id=report.id,
        position_x=data.position_x,
        position_y=data.position_y,
        width=data.width,
        height=data.height,
    )
    db.add(item)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("Report already on dashboard") from e
    await db.refresh(d)
    return DashboardRead.model_validate(d)


async def update_item(
    db: AsyncSession,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    item_id: uuid.UUID,
    data: DashboardItemUpdate,
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot edit dashboards")
    d = await get_dashboard(db, current, dashboard_id)
    item = (
        await db.execute(
            select(DashboardReport).where(
                DashboardReport.id == item_id,
                DashboardReport.dashboard_id == d.id,
            )
        )
    ).scalar_one_or_none()
    if not item:
        raise NotFound("Dashboard item not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(item, k, v)
    await db.commit()
    await db.refresh(d)
    return DashboardRead.model_validate(d)


async def remove_item(
    db: AsyncSession,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    item_id: uuid.UUID,
) -> None:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot edit dashboards")
    d = await get_dashboard(db, current, dashboard_id)
    item = (
        await db.execute(
            select(DashboardReport).where(
                DashboardReport.id == item_id,
                DashboardReport.dashboard_id == d.id,
            )
        )
    ).scalar_one_or_none()
    if not item:
        raise NotFound("Dashboard item not found")
    await db.delete(item)
    await db.commit()
