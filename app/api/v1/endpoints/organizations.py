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

from pydantic import BaseModel
from app.schemas.user import InviteRequest, InviteResponse
from app.services import invitation_service
from fastapi import status

router = APIRouter(prefix="/organizations", tags=["organizations"])


class OnboardingPayload(BaseModel):
    organizationName: str
    industry: str = ""
    erpSystem: str = ""
    teamSize: str = ""


@router.post("/onboarding")
async def complete_onboarding(
    data: OnboardingPayload,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    # 1. Update organization name
    org = (
        await db.execute(select(Organization).where(Organization.id == current.org_id))
    ).scalar_one()
    org.name = data.organizationName
    
    # Commit changes
    await db.commit()
    await db.refresh(org)
    
    # 2. Build session user
    email_name = current.email.split("@")[0].capitalize()
    user_data = {
        "id": str(current.user_id),
        "org_id": str(current.org_id),
        "email": current.email,
        "role": current.role,
        "status": "active",
        "name": email_name,
        "company": org.name,
        "organizationId": str(current.org_id),
        "organizationName": org.name,
        "onboardingCompleted": True,
    }
    
    org_data = {
        "id": str(org.id),
        "name": org.name,
        "industry": data.industry,
        "erpSystem": data.erpSystem,
        "teamSize": data.teamSize,
    }
    
    return {
        "user": user_data,
        "organization": org_data,
    }


@router.post("/invite", response_model=InviteResponse, status_code=status.HTTP_202_ACCEPTED)
async def invite(
    data: InviteRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> InviteResponse:
    return await invitation_service.invite(
        db,
        current_user_id=current.user_id,
        current_org_id=current.org_id,
        current_role=current.role,
        data=data,
    )


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

