from __future__ import annotations

import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.models import Report, ReportColumn
from app.core.exceptions import Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.query_engine.executor import execute_collect
from app.query_engine.parameter_binder import bind
from app.query_engine.template_loader import get_template_registry
from app.schemas.report import (
    ReportColumnRead,
    ReportCreate,
    ReportRead,
    ReportUpdate,
    RunReportRequest,
    RunReportResponse,
)
from app.services import connection_service


async def list_reports(db: AsyncSession, current: CurrentUser) -> list[ReportRead]:
    rows = (
        await db.execute(
            select(Report).where(Report.org_id == current.org_id).order_by(
                Report.created_at.desc()
            )
        )
    ).scalars().all()
    return [ReportRead.model_validate(r) for r in rows]


async def get_report(
    db: AsyncSession, current: CurrentUser, report_id: uuid.UUID
) -> Report:
    r = (
        await db.execute(
            select(Report).where(Report.id == report_id, Report.org_id == current.org_id)
        )
    ).scalar_one_or_none()
    if not r:
        raise NotFound("Report not found")
    return r


async def create_report(
    db: AsyncSession, current: CurrentUser, data: ReportCreate
) -> ReportRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create reports")
    # Validate the referenced template exists
    get_template_registry().get(data.query_template_id)

    r = Report(
        org_id=current.org_id,
        created_by=current.user_id,
        name=data.name,
        description=data.description,
        query_template_id=data.query_template_id,
        parameters=data.parameters,
        is_public=data.is_public,
    )
    db.add(r)
    await db.flush()
    for c in data.columns:
        db.add(
            ReportColumn(
                report_id=r.id,
                column_name=c.column_name,
                display_name=c.display_name,
                position=c.position,
                is_visible=c.is_visible,
                data_type=c.data_type,
                format_config=c.format_config,
            )
        )
    await db.commit()
    await db.refresh(r)
    return ReportRead.model_validate(r)


async def update_report(
    db: AsyncSession, current: CurrentUser, report_id: uuid.UUID, data: ReportUpdate
) -> ReportRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot update reports")
    r = await get_report(db, current, report_id)
    payload = data.model_dump(exclude_unset=True)
    cols = payload.pop("columns", None)
    for k, v in payload.items():
        setattr(r, k, v)
    if cols is not None:
        for c in list(r.columns):
            await db.delete(c)
        await db.flush()
        for c in cols:
            db.add(ReportColumn(report_id=r.id, **c))
    await db.commit()
    await db.refresh(r)
    return ReportRead.model_validate(r)


async def delete_report(
    db: AsyncSession, current: CurrentUser, report_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete reports")
    r = await get_report(db, current, report_id)
    await db.delete(r)
    await db.commit()


async def run_report(
    db: AsyncSession,
    current: CurrentUser,
    report_id: uuid.UUID,
    data: RunReportRequest,
) -> RunReportResponse:
    r = await get_report(db, current, report_id)
    conn = await connection_service.get_connection(db, current, data.connection_id)
    template = get_template_registry().get(r.query_template_id)
    merged = {**r.parameters, **data.overrides}
    bound = bind(template, merged, db_type=conn.db_type.value)

    started = time.perf_counter()
    result = await execute_collect(conn, bound)
    exec_ms = int((time.perf_counter() - started) * 1000)

    return RunReportResponse(
        report_id=r.id,
        rows=result.rows,
        columns=[ReportColumnRead.model_validate(c) for c in r.columns],
        rows_returned=result.rows_returned,
        execution_time_ms=exec_ms,
    )
