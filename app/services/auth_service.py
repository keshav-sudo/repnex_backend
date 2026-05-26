from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.database.models import Organization, PlanType, User, UserRole, UserStatus
from app.core.exceptions import Conflict, Unauthorized
from app.core.redis import get_redis
from app.core.security.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.core.security.passwords import hash_password, verify_password
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    OrgPublic,
    SignupRequest,
    TokenPair,
    UserPublic,
)


async def signup(db: AsyncSession, data: SignupRequest) -> AuthResponse:
    org = Organization(name=data.org_name, plan_type=PlanType.free)
    db.add(org)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("Organization name already taken") from e

    user = User(
        org_id=org.id,
        email=data.email.lower(),
        hashed_password=hash_password(data.password),
        role=UserRole.admin,
        status=UserStatus.active,
    )
    db.add(user)
    await db.flush()

    org.owner_id = user.id
    await db.commit()
    await db.refresh(user)
    await db.refresh(org)

    return _build_auth(user, org)


async def login(db: AsyncSession, data: LoginRequest) -> AuthResponse:
    stmt = select(User).where(User.email == data.email.lower())
    user = (await db.execute(stmt)).scalar_one_or_none()
    if not user or not user.hashed_password or not verify_password(
        data.password, user.hashed_password
    ):
        raise Unauthorized("Invalid credentials")
    if user.status != UserStatus.active:
        raise Unauthorized("User is not active")

    org = (
        await db.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one()
    return _build_auth(user, org)


async def refresh(db: AsyncSession, refresh_token: str) -> TokenPair:
    payload = decode_token(refresh_token, expected_type="refresh")
    jti = payload["jti"]
    r = get_redis()
    if await r.exists(f"jwt:denylist:{jti}"):
        raise Unauthorized("Refresh token revoked")

    user_id = uuid.UUID(payload["sub"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or user.status != UserStatus.active:
        raise Unauthorized("User invalid")

    # Rotate: denylist old jti for its remaining ttl
    await r.set(f"jwt:denylist:{jti}", "1", ex=60 * 60 * 24 * 14)

    access = create_access_token(
        user_id=user.id, org_id=user.org_id, email=user.email, role=user.role.value
    )
    new_refresh = create_refresh_token(user_id=user.id, org_id=user.org_id)
    return TokenPair(access_token=access, refresh_token=new_refresh)


async def logout(refresh_token: str) -> None:
    payload = decode_token(refresh_token, expected_type="refresh")
    await get_redis().set(
        f"jwt:denylist:{payload['jti']}", "1", ex=60 * 60 * 24 * 14
    )


def _build_auth(user: User, org: Organization) -> AuthResponse:
    access = create_access_token(
        user_id=user.id, org_id=user.org_id, email=user.email, role=user.role.value
    )
    refresh_t = create_refresh_token(user_id=user.id, org_id=user.org_id)
    return AuthResponse(
        tokens=TokenPair(access_token=access, refresh_token=refresh_t),
        user=UserPublic.model_validate(user),
        org=OrgPublic.model_validate(org),
    )
