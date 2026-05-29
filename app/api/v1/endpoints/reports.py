from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.rate_limit import rate_limit
from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.report import (
    ReportCreate,
    ReportRead,
    ReportUpdate,
    RunReportRequest,
    RunReportResponse,
)
from app.services import report_service

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


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete(
    report_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await report_service.delete_report(db, current, report_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{report_id}/run", response_model=RunReportResponse)
async def run(
    report_id: uuid.UUID,
    data: RunReportRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("query")),
) -> RunReportResponse:
    return await report_service.run_report(db, current, report_id, data)
