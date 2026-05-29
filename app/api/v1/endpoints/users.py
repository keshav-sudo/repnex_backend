from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.rate_limit import rate_limit
from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.user import (
    InviteRequest,
    InviteResponse,
    PasswordChangeRequest,
    RoleUpdateRequest,
    UserRead,
)
from app.services import invitation_service, user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead)
async def me(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    return await user_service.get_me(db, current)


@router.get("", response_model=list[UserRead])
async def list_users(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> list[UserRead]:
    return await user_service.list_org_users(db, current)


@router.post("/invite", response_model=InviteResponse, status_code=status.HTTP_202_ACCEPTED)
async def invite(
    data: InviteRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> InviteResponse:
    return await invitation_service.invite(
        db,
        current_user_id=current.user_id,
        current_org_id=current.org_id,
        current_role=current.role,
        data=data,
    )


@router.patch("/{user_id}/role", response_model=UserRead)
async def change_role(
    user_id: uuid.UUID,
    data: RoleUpdateRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    return await user_service.update_role(db, current, user_id, data)


@router.post("/me/password", status_code=status.HTTP_200_OK)
async def change_password(
    data: PasswordChangeRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("auth")),
) -> dict:
    await user_service.change_password(db, current, data.current_password, data.new_password)
    return {"ok": True}
