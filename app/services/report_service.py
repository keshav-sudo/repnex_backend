from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.models import Report, ReportColumn, ReportSnapshot
from app.core.exceptions import Forbidden, NotFound
from app.core.logging import get_logger
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
    ScheduleRequest,
    SnapshotDetailRead,
    SnapshotRead,
)
from app.services import connection_service

log = get_logger(__name__)


# ── List / Get ────────────────────────────────────────────────────────────────

async def list_reports(db: AsyncSession, current: CurrentUser) -> list[ReportRead]:
    rows = (
        (
            await db.execute(
                select(Report)
                .where(Report.org_id == current.org_id)
                .order_by(Report.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ReportRead.model_validate(r) for r in rows]


async def get_report(db: AsyncSession, current: CurrentUser, report_id: uuid.UUID) -> Report:
    r = (
        await db.execute(
            select(Report).where(Report.id == report_id, Report.org_id == current.org_id)
        )
    ).scalar_one_or_none()
    if not r:
        raise NotFound("Report not found")
    return r


# ── Create / Update / Delete ──────────────────────────────────────────────────

async def create_report(db: AsyncSession, current: CurrentUser, data: ReportCreate) -> ReportRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create reports")

    # Validate template — try static registry first, then Pinecone.
    # If neither has it, still allow save (template may be dynamic/user-created).
    registry = get_template_registry()
    if registry.has(data.query_template_id):
        registry.get(data.query_template_id)
    else:
        log.info(
            "report_template_not_in_registry",
            extra={"template_id": data.query_template_id},
        )

    r = Report(
        org_id=current.org_id,
        created_by=current.user_id,
        name=data.name,
        description=data.description,
        query_template_id=data.query_template_id,
        parameters=data.parameters,
        is_public=data.is_public,
        is_pinned=data.is_pinned,
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


async def delete_report(db: AsyncSession, current: CurrentUser, report_id: uuid.UUID) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete reports")
    r = await get_report(db, current, report_id)
    await db.delete(r)
    await db.commit()


# ── Run Report (original — does NOT save snapshot) ────────────────────────────

async def run_report(
    db: AsyncSession,
    current: CurrentUser,
    report_id: uuid.UUID,
    data: RunReportRequest,
) -> RunReportResponse:
    r = await get_report(db, current, report_id)
    conn = await connection_service.get_connection(db, current, data.connection_id)
    registry = get_template_registry()
    if registry.has(r.query_template_id):
        template = registry.get(r.query_template_id)
    else:
        from app.core.pinecone_client import get_pinecone_store_optional
        from app.query_engine.template_loader import create_template_from_pinecone

        store = get_pinecone_store_optional()
        template = None
        if store:
            meta = store.get_template_by_id(r.query_template_id)
            if meta:
                template = create_template_from_pinecone(meta)

        if not template:
            log.warning("template_not_found_falling_back_to_sales_overview", extra={"template_id": r.query_template_id})
            template = registry.get("sales_overview")
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


# ── Scheduled Refresh ─────────────────────────────────────────────────────────

async def set_schedule(
    db: AsyncSession,
    current: CurrentUser,
    report_id: uuid.UUID,
    data: ScheduleRequest,
) -> ReportRead:
    """Set or clear the auto-refresh schedule for a report."""
    if current.role == "viewer":
        raise Forbidden("Viewers cannot modify report schedules")

    r = await get_report(db, current, report_id)
    r.refresh_interval_days = data.interval_days
    r.auto_refresh_connection_id = data.connection_id

    if data.interval_days and data.interval_days > 0 and data.connection_id:
        r.next_refresh_at = datetime.now(UTC) + timedelta(days=data.interval_days)
    else:
        # Disable schedule
        r.next_refresh_at = None

    await db.commit()
    await db.refresh(r)
    log.info(
        "report_schedule_updated",
        extra={
            "report_id": str(report_id),
            "interval_days": data.interval_days,
        },
    )
    return ReportRead.model_validate(r)


async def _execute_and_snapshot(
    db: AsyncSession,
    r: Report,
    connection_id: uuid.UUID,
    triggered_by: str = "manual",
) -> ReportSnapshot:
    """Internal helper: run the query, save result as a snapshot, update timestamps."""
    conn = await connection_service.get_connection_by_id(db, connection_id)
    registry = get_template_registry()
    if registry.has(r.query_template_id):
        template = registry.get(r.query_template_id)
    else:
        from app.core.pinecone_client import get_pinecone_store_optional
        from app.query_engine.template_loader import create_template_from_pinecone

        store = get_pinecone_store_optional()
        template = None
        if store:
            meta = store.get_template_by_id(r.query_template_id)
            if meta:
                template = create_template_from_pinecone(meta)

        if not template:
            log.warning(
                "snapshot_template_not_found_fallback",
                extra={"template_id": r.query_template_id, "report_id": str(r.id)},
            )
            template = registry.get("sales_overview")

    bound = bind(template, r.parameters or {}, db_type=conn.db_type.value)

    started = time.perf_counter()
    result = await execute_collect(conn, bound)
    exec_ms = int((time.perf_counter() - started) * 1000)

    snap = ReportSnapshot(
        report_id=r.id,
        org_id=r.org_id,
        triggered_by=triggered_by,
        rows_data=result.rows,
        rows_returned=result.rows_returned,
        execution_time_ms=exec_ms,
    )
    db.add(snap)

    # Update report timestamps + schedule next run
    r.last_refreshed_at = datetime.now(UTC)
    if r.refresh_interval_days and r.refresh_interval_days > 0:
        r.next_refresh_at = datetime.now(UTC) + timedelta(days=r.refresh_interval_days)

    await db.commit()
    await db.refresh(snap)
    return snap


async def manual_refresh(
    db: AsyncSession,
    current: CurrentUser,
    report_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> SnapshotDetailRead:
    """Manually trigger a refresh — saves result as a snapshot."""
    if current.role == "viewer":
        raise Forbidden("Viewers cannot refresh reports")
    r = await get_report(db, current, report_id)
    snap = await _execute_and_snapshot(db, r, connection_id, triggered_by="manual")
    return SnapshotDetailRead(
        id=snap.id,
        report_id=snap.report_id,
        org_id=snap.org_id,
        triggered_by=snap.triggered_by,
        rows_returned=snap.rows_returned,
        execution_time_ms=snap.execution_time_ms,
        created_at=snap.created_at,
        rows_data=snap.rows_data,
    )


async def list_snapshots(
    db: AsyncSession,
    current: CurrentUser,
    report_id: uuid.UUID,
    limit: int = 20,
) -> list[SnapshotRead]:
    """Return the N most recent snapshots for a report (metadata only, no row data)."""
    r = await get_report(db, current, report_id)
    rows = (
        await db.execute(
            select(ReportSnapshot)
            .where(ReportSnapshot.report_id == r.id)
            .order_by(ReportSnapshot.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        SnapshotRead(
            id=s.id,
            report_id=s.report_id,
            org_id=s.org_id,
            triggered_by=s.triggered_by,
            rows_returned=s.rows_returned,
            execution_time_ms=s.execution_time_ms,
            created_at=s.created_at,
        )
        for s in rows
    ]


async def get_snapshot_detail(
    db: AsyncSession,
    current: CurrentUser,
    report_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> SnapshotDetailRead:
    """Return a single snapshot including full row data for preview."""
    r = await get_report(db, current, report_id)
    snap = (
        await db.execute(
            select(ReportSnapshot).where(
                ReportSnapshot.id == snapshot_id,
                ReportSnapshot.report_id == r.id,
            )
        )
    ).scalar_one_or_none()
    if not snap:
        raise NotFound("Snapshot not found")
    return SnapshotDetailRead(
        id=snap.id,
        report_id=snap.report_id,
        org_id=snap.org_id,
        triggered_by=snap.triggered_by,
        rows_returned=snap.rows_returned,
        execution_time_ms=snap.execution_time_ms,
        created_at=snap.created_at,
        rows_data=snap.rows_data,
    )


# ── APScheduler background job ────────────────────────────────────────────────

async def run_due_reports(db: AsyncSession) -> None:
    """Called by APScheduler every hour.
    Finds all reports whose next_refresh_at <= now and runs them.
    """
    now = datetime.now(UTC)
    due = (
        await db.execute(
            select(Report)
            .where(
                Report.next_refresh_at <= now,
                Report.refresh_interval_days > 0,
                Report.auto_refresh_connection_id.is_not(None),
            )
        )
    ).scalars().all()

    log.info("scheduled_refresh_scan", extra={"due_count": len(due)})

    for r in due:
        try:
            await _execute_and_snapshot(
                db, r, r.auto_refresh_connection_id, triggered_by="scheduled"
            )
            log.info(
                "scheduled_refresh_success",
                extra={"report_id": str(r.id), "name": r.name},
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "scheduled_refresh_failed",
                extra={"report_id": str(r.id), "error": str(exc)},
            )
