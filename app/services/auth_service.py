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
    # Derive organization name from email if company field was left blank
    org_name = data.org_name.strip() if data.org_name.strip() else data.email.split("@")[0].capitalize()
    org = Organization(name=org_name, plan_type=PlanType.free)
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
    result = await db.execute(stmt)
    users = result.scalars().all()
    
    matching_user = None
    for u in users:
        if u.hashed_password and verify_password(data.password, u.hashed_password):
            matching_user = u
            break
            
    if not matching_user:
        raise Unauthorized("Invalid credentials")
        
    if matching_user.status != UserStatus.active:
        raise Unauthorized("User is not active")

    org = (
        await db.execute(select(Organization).where(Organization.id == matching_user.org_id))
    ).scalar_one()
    return _build_auth(matching_user, org)


async def refresh(db: AsyncSession, refresh_token: str) -> TokenPair:
    payload = decode_token(refresh_token, expected_type="refresh")
    jti = payload["jti"]
    r = get_redis()
    if r is not None and await r.exists(f"jwt:denylist:{jti}"):
        raise Unauthorized("Refresh token revoked")

    user_id = uuid.UUID(payload["sub"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or user.status != UserStatus.active:
        raise Unauthorized("User invalid")

    if r is not None:
        await r.set(f"jwt:denylist:{jti}", "1", ex=60 * 60 * 24 * 14)

    access = create_access_token(
        user_id=user.id, org_id=user.org_id, email=user.email, role=user.role.value
    )
    new_refresh = create_refresh_token(user_id=user.id, org_id=user.org_id)
    return TokenPair(access_token=access, refresh_token=new_refresh)


async def logout(refresh_token: str) -> None:
    payload = decode_token(refresh_token, expected_type="refresh")
    r = get_redis()
    if r is not None:
        await r.set(
            f"jwt:denylist:{payload['jti']}", "1", ex=60 * 60 * 24 * 14
        )


def _build_auth(user: User, org: Organization) -> AuthResponse:
    access = create_access_token(
        user_id=user.id, org_id=user.org_id, email=user.email, role=user.role.value
    )
    refresh_t = create_refresh_token(user_id=user.id, org_id=user.org_id)
    
    # Extract name from email
    email_name = user.email.split("@")[0].capitalize()
    
    user_public = UserPublic(
        id=user.id,
        org_id=user.org_id,
        email=user.email,
        role=user.role.value,
        status=user.status.value,
        name=email_name,
        company=org.name,
        organizationId=org.id,
        organizationName=org.name,
        onboardingCompleted=True,
    )
    
    return AuthResponse(
        tokens=TokenPair(access_token=access, refresh_token=refresh_t),
        user=user_public,
        org=OrgPublic.model_validate(org),
        token=access,
    )
