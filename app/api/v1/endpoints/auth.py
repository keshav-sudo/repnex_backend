from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.security.auth import CurrentUser
from app.core.database.session import get_db
from app.schemas.auth import (
    AcceptInviteRequest,
    AuthResponse,
    ForgotPasswordRequest,
    InvitePreview,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenPair,
    UserPublic,
)
from app.services import auth_service, invitation_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(data: SignupRequest, db: AsyncSession = Depends(get_db)) -> AuthResponse:
    return await auth_service.signup(db, data)


@router.get("/session", response_model=UserPublic)
async def get_session(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> UserPublic:
    from sqlalchemy import select
    from app.core.database.models import Organization
    org = (
        await db.execute(select(Organization).where(Organization.id == current.org_id))
    ).scalar_one()
    
    email_name = current.email.split("@")[0].capitalize()
    return UserPublic(
        id=current.user_id,
        org_id=current.org_id,
        email=current.email,
        role=current.role,
        status="active",
        name=email_name,
        company=org.name,
        organizationId=current.org_id,
        organizationName=org.name,
        onboardingCompleted=True,
    )


@router.post("/login", response_model=AuthResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)) -> AuthResponse:
    return await auth_service.login(db, data)


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    data: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    return await auth_service.forgot_password(db, data.email)


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    return await auth_service.reset_password(db, data.token, data.password)


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    return await auth_service.refresh(db, data.refresh_token)


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(data: RefreshRequest) -> dict:
    await auth_service.logout(data.refresh_token)
    return {"ok": True}


@router.post("/accept-invite", response_model=AuthResponse)
async def accept_invite(
    data: AcceptInviteRequest, db: AsyncSession = Depends(get_db)
) -> AuthResponse:
    return await invitation_service.accept(db, data)


@router.get("/invite", response_model=InvitePreview)
async def invite_preview(token: str, db: AsyncSession = Depends(get_db)) -> InvitePreview:
    return await invitation_service.preview(db, token)
