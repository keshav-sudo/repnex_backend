from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.models import Organization
from app.core.database.session import get_db
from app.core.exceptions import Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.schemas.organization import OrgRead, OrgUpdate

router = APIRouter(prefix="/orgs", tags=["organizations"])


@router.get("/me", response_model=OrgRead)
async def my_org(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> OrgRead:
    org = (
        await db.execute(select(Organization).where(Organization.id == current.org_id))
    ).scalar_one_or_none()
    if not org:
        raise NotFound("Organization not found")
    return OrgRead.model_validate(org)


@router.patch("/me", response_model=OrgRead)
async def update_my_org(
    data: OrgUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> OrgRead:
    if current.role != "admin":
        raise Forbidden("Only admins can update org")
    org = (
        await db.execute(select(Organization).where(Organization.id == current.org_id))
    ).scalar_one_or_none()
    if not org:
        raise NotFound("Organization not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(org, k, v)
    await db.commit()
    await db.refresh(org)
    return OrgRead.model_validate(org)
