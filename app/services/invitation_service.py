from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database.models import Organization, User, UserRole, UserStatus
from app.core.exceptions import Conflict, Forbidden, NotFound
from app.core.security.auth import (
    create_access_token,
    create_invite_token,
    create_refresh_token,
    decode_token,
)
from app.core.security.passwords import hash_password
from app.schemas.auth import (
    AcceptInviteRequest,
    AuthResponse,
    OrgPublic,
    TokenPair,
    UserPublic,
)
from app.schemas.user import InviteRequest, InviteResponse
from app.utils.email import send_invite_email


async def invite(
    db: AsyncSession, *, current_user_id: uuid.UUID, current_org_id: uuid.UUID,
    current_role: str, data: InviteRequest,
) -> InviteResponse:
    if current_role != "admin":
        raise Forbidden("Only admins may invite")

    user = User(
        org_id=current_org_id,
        email=data.email.lower(),
        hashed_password=None,
        role=UserRole(data.role),
        status=UserStatus.pending,
        invited_by=current_user_id,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("A user with that email already exists in this org") from e

    token = create_invite_token(user_id=user.id, org_id=current_org_id)
    org = (
        await db.execute(select(Organization).where(Organization.id == current_org_id))
    ).scalar_one()
    await db.commit()

    settings = get_settings()
    accept_url = f"{settings.APP_BASE_URL}/accept-invite?token={token}"
    send_invite_email(to=user.email, accept_url=accept_url, org_name=org.name)

    return InviteResponse(user_id=user.id, status=user.status.value)


async def accept(db: AsyncSession, data: AcceptInviteRequest) -> AuthResponse:
    payload = decode_token(data.token, expected_type="invite")
    user_id = uuid.UUID(payload["sub"])
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise NotFound("Invitation invalid")
    if user.status not in (UserStatus.pending, UserStatus.expired):
        raise Conflict("Invite already accepted")

    user.hashed_password = hash_password(data.password)
    user.status = UserStatus.active
    org = (
        await db.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one()
    await db.commit()
    await db.refresh(user)

    access = create_access_token(
        user_id=user.id, org_id=user.org_id, email=user.email, role=user.role.value
    )
    refresh_t = create_refresh_token(user_id=user.id, org_id=user.org_id)
    return AuthResponse(
        tokens=TokenPair(access_token=access, refresh_token=refresh_t),
        user=UserPublic.model_validate(user),
        org=OrgPublic.model_validate(org),
    )
