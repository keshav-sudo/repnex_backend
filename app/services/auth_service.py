from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database.models import Organization, PlanType, User, UserRole, UserStatus
from app.core.exceptions import Conflict, Unauthorized
from app.core.redis import get_redis
from app.core.security.auth import (
    create_access_token,
    create_reset_token,
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
from app.utils.email import send_email_async


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


async def forgot_password(db: AsyncSession, email: str) -> dict:
    normalized_email = email.lower()
    user = (
        await db.execute(
            select(User).where(
                User.email == normalized_email,
                User.status == UserStatus.active,
                User.hashed_password.is_not(None),
            )
        )
    ).scalars().first()

    if not user:
        return {"ok": True}

    org = (
        await db.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one()
    token = create_reset_token(user_id=user.id, org_id=user.org_id, email=user.email)
    reset_url = f"{get_settings().APP_BASE_URL.rstrip('/')}/reset-password?token={token}"

    try:
        asyncio.create_task(
            send_email_async(
                to=user.email,
                subject="Reset your Repnex password",
                body_text=(
                    f"Hi {user.email.split('@')[0].capitalize()},\n\n"
                    f"We received a request to reset your Repnex password for {org.name}.\n\n"
                    f"Reset your password here: {reset_url}\n\n"
                    "This link expires in 30 minutes. If you did not request this, ignore this email."
                ),
                body_html=f"""
                <div style="font-family:'Segoe UI',sans-serif;max-width:520px;margin:40px auto;
                            background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.08);overflow:hidden;">
                  <div style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:28px 24px;text-align:center;">
                    <h1 style="margin:0;color:#fff;font-size:22px;">Reset your password</h1>
                  </div>
                  <div style="padding:30px 24px;">
                    <p style="color:#374151;font-size:15px;line-height:1.6;">
                      We received a request to reset your Repnex password for
                      <strong>{org.name}</strong>.
                    </p>
                    <div style="text-align:center;margin:28px 0;">
                      <a href="{reset_url}"
                         style="display:inline-block;background:linear-gradient(135deg,#2563eb,#1d4ed8);
                                color:#fff;text-decoration:none;padding:14px 32px;border-radius:12px;
                                font-size:15px;font-weight:600;">
                        Reset Password
                      </a>
                    </div>
                    <p style="color:#6b7280;font-size:12px;line-height:1.5;">
                      This link expires in 30 minutes. If you did not request this, you can ignore this email.
                    </p>
                  </div>
                </div>
                """,
            ),
            name=f"password_reset_email_{user.id}",
        )
    except RuntimeError:
        await send_email_async(
            to=user.email,
            subject="Reset your Repnex password",
            body_text=f"Reset your Repnex password here: {reset_url}",
        )

    return {"ok": True}


async def reset_password(db: AsyncSession, token: str, password: str) -> dict:
    payload = decode_token(token, expected_type="reset")
    jti = payload["jti"]
    r = get_redis()
    if r is not None and await r.exists(f"jwt:denylist:{jti}"):
        raise Unauthorized("Reset link already used")

    user_id = uuid.UUID(payload["sub"])
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user or user.status != UserStatus.active:
        raise Unauthorized("Invalid reset link")

    user.hashed_password = hash_password(password)
    await db.commit()

    if r is not None:
        await r.set(f"jwt:denylist:{jti}", "1", ex=60 * 60)

    return {"ok": True}


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
