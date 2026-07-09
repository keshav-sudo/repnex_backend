"""report_service — saved reports and snapshots using the semantic engine.

Reports store the natural language query and executed SQL.
Snapshots capture the result rows at point-in-time for display.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database.models import Report, ReportColumn, ReportSnapshot
from app.core.exceptions import Forbidden, NotFound
from app.core.logging import get_logger
from app.core.security.auth import CurrentUser
from app.engine import BoundQuery, execute_collect
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
    doc = await db[Report.COLLECTION].find_one(
        {"_id": str(report_id), "org_id": str(current.org_id)}
    )
    if not doc:
        raise NotFound("Report not found")
    return Report(**doc)


# ── Create / Update / Delete ──────────────────────────────────────────────────

async def create_report(
    db: AsyncIOMotorDatabase, current: CurrentUser, data: ReportCreate
) -> ReportRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create reports")

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
    await get_report(db, current, report_id)

    payload = data.model_dump(exclude_unset=True)
    cols = payload.pop("columns", None)
    update_fields = dict(payload)

    if cols is not None:
        update_fields["columns"] = [
            {
                "id": str(uuid.uuid4()),
                "column_name": c["column_name"],
                "display_name": c["display_name"],
                "position": c["position"],
                "is_visible": c["is_visible"],
                "data_type": c["data_type"],
                "format_config": c["format_config"],
            }
            for c in cols
        ]

    if update_fields:
        await db[Report.COLLECTION].update_one(
            {"_id": str(report_id)}, {"$set": update_fields}
        )

    updated_doc = await db[Report.COLLECTION].find_one({"_id": str(report_id)})
    return ReportRead.model_validate(Report(**updated_doc))


async def delete_report(
    db: AsyncIOMotorDatabase, current: CurrentUser, report_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete reports")
    await get_report(db, current, report_id)
    await db[Report.COLLECTION].delete_one({"_id": str(report_id)})
    await db[ReportSnapshot.COLLECTION].delete_many({"report_id": str(report_id)})


# ── Run Report ────────────────────────────────────────────────────────────────

async def run_report(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    report_id: uuid.UUID,
    data: RunReportRequest,
) -> RunReportResponse:
    """Execute a saved report using its stored SQL and save a snapshot."""
    r = await get_report(db, current, report_id)
    conn = await connection_service.get_connection(db, current, data.connection_id)

    # Reports store the pre-generated SQL directly in parameters["sql"]
    sql = (data.overrides or {}).get("sql") or (r.parameters or {}).get("sql", "")
    if not sql:
        raise NotFound("Report has no executable SQL. Re-run the query to generate one.")

    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)

    started = time.perf_counter()
    result = await execute_collect(conn, bound)
    exec_ms = int((time.perf_counter() - started) * 1000)

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
            {"$set": {"last_refreshed_at": datetime.now(timezone.utc)}},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot_save_failed", extra={"report_id": str(r.id), "err": str(exc)})

    cols = [ReportColumnRead.model_validate(c) for c in r.columns]
    if not cols and result.rows:
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
            for idx, name in enumerate(result.rows[0].keys())
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
    if current.role == "viewer":
        raise Forbidden("Viewers cannot modify report schedules")
    await get_report(db, current, report_id)

    update_fields: dict = {
        "refresh_interval_days": data.interval_days,
        "auto_refresh_connection_id": str(data.connection_id) if data.connection_id else None,
    }
    if data.interval_days and data.interval_days > 0 and data.connection_id:
        update_fields["next_refresh_at"] = datetime.now(timezone.utc) + timedelta(
            days=data.interval_days
        )
    else:
        update_fields["next_refresh_at"] = None

    await db[Report.COLLECTION].update_one({"_id": str(report_id)}, {"$set": update_fields})
    log.info("report_schedule_updated", extra={"report_id": str(report_id)})
    updated_doc = await db[Report.COLLECTION].find_one({"_id": str(report_id)})
    return ReportRead.model_validate(Report(**updated_doc))


async def _execute_and_snapshot(
    db: AsyncIOMotorDatabase,
    r: Report,
    connection_id: uuid.UUID,
    triggered_by: str = "manual",
) -> ReportSnapshot:
    conn = await connection_service.get_connection_by_id(db, connection_id)
    sql = (r.parameters or {}).get("sql", "")
    if not sql:
        raise ValueError(f"Report {r.id} has no stored SQL for scheduled refresh.")

    bound = BoundQuery(sql=sql, params={}, db_type=conn.db_type.value)
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

    now = datetime.now(timezone.utc)
    update_fields: dict = {"last_refreshed_at": now}
    if r.refresh_interval_days and r.refresh_interval_days > 0:
        update_fields["next_refresh_at"] = now + timedelta(days=r.refresh_interval_days)
    await db[Report.COLLECTION].update_one({"_id": str(r.id)}, {"$set": update_fields})

    return ReportSnapshot(**snap_doc)


async def manual_refresh(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    report_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> SnapshotDetailRead:
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
    r = await get_report(db, current, report_id)
    cursor = db[ReportSnapshot.COLLECTION].find(
        {"report_id": str(r.id)}, projection={"rows_data": 0}
    )
    rows = await cursor.sort("created_at", -1).limit(limit).to_list(length=limit)
    return [SnapshotRead.model_validate(ReportSnapshot(**s)) for s in rows]


async def get_snapshot_detail(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    report_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> SnapshotDetailRead:
    r = await get_report(db, current, report_id)
    snap = await db[ReportSnapshot.COLLECTION].find_one(
        {"_id": str(snapshot_id), "report_id": str(r.id)}
    )
    if not snap:
        raise NotFound("Snapshot not found")
    return SnapshotDetailRead.model_validate(ReportSnapshot(**snap))


# ── APScheduler background job ────────────────────────────────────────────────

async def run_due_reports(db: AsyncIOMotorDatabase) -> None:
    """Called by APScheduler every hour — runs all overdue scheduled reports."""
    now = datetime.now(timezone.utc)
    cursor = db[Report.COLLECTION].find({
        "next_refresh_at": {"$lte": now},
        "refresh_interval_days": {"$gt": 0},
        "auto_refresh_connection_id": {"$ne": None},
    })
    due = await cursor.to_list(length=1000)
    log.info("scheduled_refresh_scan", extra={"due_count": len(due)})

    for r_doc in due:
        r = Report(**r_doc)
        try:
            await _execute_and_snapshot(
                db, r, uuid.UUID(r_doc["auto_refresh_connection_id"]), triggered_by="scheduled"
            )
            log.info("scheduled_refresh_success", extra={"report_id": str(r.id)})
        except Exception as exc:  # noqa: BLE001
            log.error("scheduled_refresh_failed", extra={"report_id": str(r.id), "err": str(exc)})
