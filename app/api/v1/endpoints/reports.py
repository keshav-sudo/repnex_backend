from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.rate_limit import rate_limit
from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.report import (
    ReportCreate,
    ReportRead,
    ReportUpdate,
    RefreshRequest,
    RunReportRequest,
    RunReportResponse,
    ScheduleRequest,
    SnapshotDetailRead,
    SnapshotRead,
    ReportExportRequest,
    BulkExportRequest,
)
from app.services import report_service, export_service

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_model=list[ReportRead])
async def list_(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> list[ReportRead]:
    return await report_service.list_reports(db, current)


@router.post("", response_model=ReportRead, status_code=status.HTTP_201_CREATED)
async def create(
    data: ReportCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    return await report_service.create_report(db, current, data)


@router.post("/export/excel")
async def export_excel(
    data: ReportExportRequest,
    current: CurrentUser = Depends(bind_tenant_context),
) -> StreamingResponse:
    """Export report data to Excel (.xlsx)."""
    excel_bytes = export_service.generate_excel(data.title, data.headers, data.rows)
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=export.xlsx"},
    )


@router.post("/export/pdf")
async def export_pdf(
    data: ReportExportRequest,
    current: CurrentUser = Depends(bind_tenant_context),
) -> StreamingResponse:
    """Export report data to PDF (.pdf)."""
    pdf_bytes = export_service.generate_pdf(data.title, data.headers, data.rows)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=export.pdf"},
    )


@router.post("/export/bulk")
async def export_bulk(
    data: BulkExportRequest,
    current: CurrentUser = Depends(bind_tenant_context),
) -> StreamingResponse:
    """Export multiple reports combined into a single file or a ZIP package."""
    reports_dict = [{"title": r.title, "headers": r.headers, "rows": r.rows} for r in data.reports]
    
    if data.format == "excel":
        file_bytes = export_service.generate_bulk_excel(reports_dict)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "bulk_export.xlsx"
    elif data.format == "pdf":
        file_bytes = export_service.generate_bulk_pdf(reports_dict)
        media_type = "application/pdf"
        filename = "bulk_export.pdf"
    else:  # zip or csv
        # If format is csv, we package individual CSVs in a ZIP
        file_bytes = export_service.generate_bulk_zip(reports_dict, format_type="csv" if data.format == "csv" else "excel")
        media_type = "application/zip"
        filename = "bulk_export.zip"
        
    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{report_id}", response_model=ReportRead)
async def get(
    report_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    r = await report_service.get_report(db, current, report_id)
    return ReportRead.model_validate(r)


@router.patch("/{report_id}", response_model=ReportRead)
async def update(
    report_id: uuid.UUID,
    data: ReportUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    return await report_service.update_report(db, current, report_id, data)


@router.patch("/{report_id}/pin", response_model=ReportRead)
async def toggle_pin(
    report_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    r = await report_service.get_report(db, current, report_id)
    r.is_pinned = not r.is_pinned
    await db.commit()
    await db.refresh(r)
    return ReportRead.model_validate(r)


@router.delete("/{report_id}", status_code=status.HTTP_200_OK)
async def delete(
    report_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await report_service.delete_report(db, current, report_id)
    return {"ok": True}


@router.post("/{report_id}/run", response_model=RunReportResponse)
async def run(
    report_id: uuid.UUID,
    data: RunReportRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("query")),
) -> RunReportResponse:
    return await report_service.run_report(db, current, report_id, data)


# ── Schedule & Snapshot endpoints ─────────────────────────────────────────────

@router.patch("/{report_id}/schedule", response_model=ReportRead)
async def set_schedule(
    report_id: uuid.UUID,
    data: ScheduleRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    """Set or clear the auto-refresh schedule for a report.

    - `interval_days` = 0 or null → disable auto-refresh
    - `interval_days` = 1/2/3 → refresh every N days
    - `connection_id` → which DB connection to use for the scheduled run
    """
    return await report_service.set_schedule(db, current, report_id, data)


@router.post(
    "/{report_id}/refresh",
    response_model=SnapshotDetailRead,
    status_code=status.HTTP_201_CREATED,
)
async def manual_refresh(
    report_id: uuid.UUID,
    data: RefreshRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("query")),
) -> SnapshotDetailRead:
    """Manually trigger a report refresh and save result as a snapshot."""
    return await report_service.manual_refresh(db, current, report_id, data.connection_id)


@router.get("/{report_id}/snapshots", response_model=list[SnapshotRead])
async def list_snapshots(
    report_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> list[SnapshotRead]:
    """List historical run snapshots (metadata only, no row data)."""
    return await report_service.list_snapshots(db, current, report_id, limit=limit)


@router.get(
    "/{report_id}/snapshots/{snapshot_id}",
    response_model=SnapshotDetailRead,
)
async def get_snapshot(
    report_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> SnapshotDetailRead:
    """Get a single snapshot including full row data for preview."""
    return await report_service.get_snapshot_detail(db, current, report_id, snapshot_id)

