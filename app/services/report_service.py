from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorDatabase

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

async def list_reports(db: AsyncIOMotorDatabase, current: CurrentUser) -> list[ReportRead]:
    cursor = db[Report.COLLECTION].find({"org_id": str(current.org_id)})
    rows = await cursor.sort("created_at", -1).to_list(length=1000)
    return [ReportRead.model_validate(Report(**r)) for r in rows]


async def get_report(db: AsyncIOMotorDatabase, current: CurrentUser, report_id: uuid.UUID) -> Report:
    doc = await db[Report.COLLECTION].find_one({
        "_id": str(report_id),
        "org_id": str(current.org_id)
    })
    if not doc:
        raise NotFound("Report not found")
    return Report(**doc)


# ── Create / Update / Delete ──────────────────────────────────────────────────

async def create_report(db: AsyncIOMotorDatabase, current: CurrentUser, data: ReportCreate) -> ReportRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create reports")

    registry = get_template_registry()
    template = None
    if registry.has(data.query_template_id):
        template = registry.get(data.query_template_id)
    else:
        log.info(
            "report_template_not_in_registry",
            extra={"template_id": data.query_template_id},
        )
        from app.core.pinecone_client import get_pinecone_store_optional
        from app.query_engine.template_loader import create_template_from_pinecone
        store = get_pinecone_store_optional()
        if store:
            meta = store.get_template_by_id(data.query_template_id)
            if meta:
                template = create_template_from_pinecone(meta)

    cols_list = []
    if data.columns:
        for c in data.columns:
            cols_list.append({
                "id": str(uuid.uuid4()),
                "column_name": c.column_name,
                "display_name": c.display_name,
                "position": c.position,
                "is_visible": c.is_visible,
                "data_type": c.data_type,
                "format_config": c.format_config,
            })
    elif template and template.result_columns:
        for idx, col_name in enumerate(template.result_columns):
            cols_list.append({
                "id": str(uuid.uuid4()),
                "column_name": col_name,
                "display_name": col_name.replace("_", " ").title(),
                "position": idx,
                "is_visible": True,
                "data_type": "string",
                "format_config": {},
            })

    report_doc = Report.new(
        org_id=str(current.org_id),
        created_by=str(current.user_id),
        name=data.name,
        description=data.description,
        query_template_id=data.query_template_id,
        parameters=data.parameters,
        is_public=data.is_public,
        is_pinned=data.is_pinned,
    )
    report_doc["columns"] = cols_list

    await db[Report.COLLECTION].insert_one(report_doc)
    return ReportRead.model_validate(Report(**report_doc))


async def update_report(
    db: AsyncIOMotorDatabase, current: CurrentUser, report_id: uuid.UUID, data: ReportUpdate
) -> ReportRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot update reports")
    r = await get_report(db, current, report_id)

    payload = data.model_dump(exclude_unset=True)
    cols = payload.pop("columns", None)

    update_fields = {}
    for k, v in payload.items():
        update_fields[k] = v

    if cols is not None:
        cols_list = []
        for c in cols:
            cols_list.append({
                "id": str(uuid.uuid4()),
                "column_name": c["column_name"],
                "display_name": c["display_name"],
                "position": c["position"],
                "is_visible": c["is_visible"],
                "data_type": c["data_type"],
                "format_config": c["format_config"],
            })
        update_fields["columns"] = cols_list

    if update_fields:
        await db[Report.COLLECTION].update_one(
            {"_id": str(report_id)},
            {"$set": update_fields}
        )

    updated_doc = await db[Report.COLLECTION].find_one({"_id": str(report_id)})
    return ReportRead.model_validate(Report(**updated_doc))


async def delete_report(db: AsyncIOMotorDatabase, current: CurrentUser, report_id: uuid.UUID) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete reports")
    r = await get_report(db, current, report_id)
    await db[Report.COLLECTION].delete_one({"_id": str(report_id)})
    await db[ReportSnapshot.COLLECTION].delete_many({"report_id": str(report_id)})


# ── Run Report (original — saves snapshot) ────────────────────────────

async def run_report(
    db: AsyncIOMotorDatabase,
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

    # Save snapshot in database
    try:
        snap_doc = ReportSnapshot.new(
            report_id=str(r.id),
            org_id=str(r.org_id),
            triggered_by="manual",
            rows_data=result.rows,
            rows_returned=result.rows_returned,
            execution_time_ms=exec_ms,
        )
        await db[ReportSnapshot.COLLECTION].insert_one(snap_doc)

        await db[Report.COLLECTION].update_one(
            {"_id": str(r.id)},
            {"$set": {"last_refreshed_at": datetime.now(timezone.utc)}}
        )
    except Exception as e:
        log.warning("failed_to_save_run_snapshot", extra={"report_id": str(r.id), "error": str(e)})

    cols = [ReportColumnRead.model_validate(c) for c in r.columns]
    if not cols:
        col_names = []
        if template and template.result_columns:
            col_names = list(template.result_columns)
        elif result.rows:
            col_names = list(result.rows[0].keys())

        if col_names:
            cols = [
                ReportColumnRead(
                    id=uuid.uuid4(),
                    column_name=name,
                    display_name=name.replace("_", " ").title(),
                    position=idx,
                    is_visible=True,
                    data_type="string",
                    format_config={},
                )
                for idx, name in enumerate(col_names)
            ]

    return RunReportResponse(
        report_id=r.id,
        rows=result.rows,
        columns=cols,
        rows_returned=result.rows_returned,
        execution_time_ms=exec_ms,
    )


# ── Scheduled Refresh ─────────────────────────────────────────────────────────

async def set_schedule(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    report_id: uuid.UUID,
    data: ScheduleRequest,
) -> ReportRead:
    """Set or clear the auto-refresh schedule for a report."""
    if current.role == "viewer":
        raise Forbidden("Viewers cannot modify report schedules")

    r = await get_report(db, current, report_id)

    update_fields = {
        "refresh_interval_days": data.interval_days,
        "auto_refresh_connection_id": str(data.connection_id) if data.connection_id else None
    }

    if data.interval_days and data.interval_days > 0 and data.connection_id:
        update_fields["next_refresh_at"] = datetime.now(timezone.utc) + timedelta(days=data.interval_days)
    else:
        update_fields["next_refresh_at"] = None

    await db[Report.COLLECTION].update_one(
        {"_id": str(report_id)},
        {"$set": update_fields}
    )

    updated_doc = await db[Report.COLLECTION].find_one({"_id": str(report_id)})
    log.info(
        "report_schedule_updated",
        extra={
            "report_id": str(report_id),
            "interval_days": data.interval_days,
        },
    )
    return ReportRead.model_validate(Report(**updated_doc))


async def _execute_and_snapshot(
    db: AsyncIOMotorDatabase,
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

    snap_doc = ReportSnapshot.new(
        report_id=str(r.id),
        org_id=str(r.org_id),
        triggered_by=triggered_by,
        rows_data=result.rows,
        rows_returned=result.rows_returned,
        execution_time_ms=exec_ms,
    )
    await db[ReportSnapshot.COLLECTION].insert_one(snap_doc)

    # Update report timestamps + schedule next run
    now = datetime.now(timezone.utc)
    update_fields = {"last_refreshed_at": now}
    if r.refresh_interval_days and r.refresh_interval_days > 0:
        update_fields["next_refresh_at"] = now + timedelta(days=r.refresh_interval_days)

    await db[Report.COLLECTION].update_one(
        {"_id": str(r.id)},
        {"$set": update_fields}
    )

    return ReportSnapshot(**snap_doc)


async def manual_refresh(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    report_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> SnapshotDetailRead:
    """Manually trigger a refresh — saves result as a snapshot."""
    if current.role == "viewer":
        raise Forbidden("Viewers cannot refresh reports")
    r = await get_report(db, current, report_id)
    snap = await _execute_and_snapshot(db, r, connection_id, triggered_by="manual")
    return SnapshotDetailRead.model_validate(snap)


async def list_snapshots(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    report_id: uuid.UUID,
    limit: int = 20,
) -> list[SnapshotRead]:
    """Return the N most recent snapshots for a report (metadata only, no row data)."""
    r = await get_report(db, current, report_id)
    cursor = db[ReportSnapshot.COLLECTION].find(
        {"report_id": str(r.id)},
        projection={"rows_data": 0}
    )
    rows = await cursor.sort("created_at", -1).limit(limit).to_list(length=limit)
    return [SnapshotRead.model_validate(ReportSnapshot(**s)) for s in rows]


async def get_snapshot_detail(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    report_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> SnapshotDetailRead:
    """Return a single snapshot including full row data for preview."""
    r = await get_report(db, current, report_id)
    snap = await db[ReportSnapshot.COLLECTION].find_one({
        "_id": str(snapshot_id),
        "report_id": str(r.id),
    })
    if not snap:
        raise NotFound("Snapshot not found")
    return SnapshotDetailRead.model_validate(ReportSnapshot(**snap))


# ── APScheduler background job ────────────────────────────────────────────────

async def run_due_reports(db: AsyncIOMotorDatabase) -> None:
    """Called by APScheduler every hour.
    Finds all reports whose next_refresh_at <= now and runs them.
    """
    now = datetime.now(timezone.utc)
    cursor = db[Report.COLLECTION].find({
        "next_refresh_at": {"$lte": now},
        "refresh_interval_days": {"$gt": 0},
        "auto_refresh_connection_id": {"$ne": None}
    })
    due = await cursor.to_list(length=1000)

    log.info("scheduled_refresh_scan", extra={"due_count": len(due)})

    for r_doc in due:
        r = Report(**r_doc)
        try:
            await _execute_and_snapshot(
                db, r, uuid.UUID(r_doc["auto_refresh_connection_id"]), triggered_by="scheduled"
            )
            log.info(
                "scheduled_refresh_success",
                extra={"report_id": str(r.id), "name": r.name},
            )
        except Exception as exc:
            log.error(
                "scheduled_refresh_failed",
                extra={"report_id": str(r.id), "error": str(exc)},
            )
