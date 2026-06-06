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
    
    # Send welcome email asynchronously (fire-and-forget)
    try:
        from app.utils.email import send_email_async
        import asyncio
        asyncio.create_task(
            send_email_async(
                to=current.email,
                subject=f"🎉 Welcome to Repnex — {org.name} is all set!",
                body_text=f"Hi {current.email.split('@')[0].capitalize()},\n\n"
                          f"Your organization '{org.name}' has been set up on Repnex.\n"
                          f"You can now connect your databases and start asking questions.\n\n"
                          f"Get started: https://repnex.ai/dashboard\n\n"
                          f"— The Repnex Team",
                body_html=f"""
                <div style="font-family:'Segoe UI',sans-serif;max-width:520px;margin:40px auto;
                            background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.08);overflow:hidden;">
                  <div style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:32px 24px;text-align:center;">
                    <h1 style="margin:0;color:#fff;font-size:22px;">Welcome to Repnex! 🎉</h1>
                  </div>
                  <div style="padding:32px 24px;">
                    <p style="color:#374151;font-size:15px;line-height:1.6;">
                      Hi <strong>{current.email.split('@')[0].capitalize()}</strong>,
                    </p>
                    <p style="color:#6b7280;font-size:14px;line-height:1.6;">
                      Your organization <strong>{org.name}</strong> is all set up. You can now:
                    </p>
                    <ul style="color:#6b7280;font-size:14px;line-height:2;">
                      <li>Connect your ERP databases</li>
                      <li>Ask questions in plain English</li>
                      <li>Generate instant reports & dashboards</li>
                    </ul>
                    <div style="text-align:center;margin:28px 0;">
                      <a href="https://repnex.ai/dashboard"
                         style="display:inline-block;background:linear-gradient(135deg,#2563eb,#1d4ed8);
                                color:#fff;text-decoration:none;padding:14px 36px;border-radius:12px;
                                font-size:15px;font-weight:600;">
                        Go to Dashboard →
                      </a>
                    </div>
                  </div>
                </div>
                """,
            ),
            name="onboarding_welcome_email",
        )
    except Exception:
        pass  # Non-critical — don't fail onboarding if email fails
    
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

