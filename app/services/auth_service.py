from __future__ import annotations

import random

from app.core.config import get_settings
from app.core.database.models import (
    Organization as OrgModel,
)
from app.core.database.models import (
    PlanType,
    UserRole,
    UserStatus,
)
from app.core.database.models import (
    User as UserModel,
)
from app.core.exceptions import Conflict, Unauthorized
from app.core.redis import get_redis
from app.core.security.auth import (
    create_access_token,
    create_refresh_token,
    create_reset_token,
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
from app.utils.email import fire_and_forget, send_email_async
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

_local_otps: dict[str, str] = {}


async def send_otp(db: AsyncIOMotorDatabase, email: str) -> dict:
    normalized_email = email.lower()

    # 1. Check if email already registered
    existing_user = await db[UserModel.COLLECTION].find_one({"email": normalized_email})
    if existing_user:
        raise Conflict("Email already registered")

    # 2. Generate 6-digit OTP
    code = f"{random.randint(100000, 999999)}"

    # 3. Store OTP in Redis and local fallback
    r = get_redis()
    if r is not None:
        try:
            await r.set(f"otp:{normalized_email}", code, ex=600)
        except Exception:
            pass
    _local_otps[normalized_email] = code

    # 4. Dispatch Email with OTP
    subject = f"🔐 Verify your email for Repnex - {code}"
    body_text = (
        f"Hello,\n\n"
        f"Your verification code is {code}.\n\n"
        f"This code is valid for 10 minutes. If you did not request this, you can ignore this email.\n\n"
        f"— The Repnex Team"
    )
    body_html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:480px;margin:40px auto;
                background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.08);overflow:hidden;">
      <div style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:32px 24px;text-align:center;">
        <h1 style="margin:0;color:#fff;font-size:22px;letter-spacing:0.5px;">Repnex Verification</h1>
      </div>
      <div style="padding:32px 24px;text-align:center;">
        <p style="color:#4b5563;font-size:15px;margin-bottom:24px;">
          Use the following 6-digit code to verify your email address:
        </p>
        <div style="display:inline-block;background:#f3f4f6;padding:16px 32px;border-radius:12px;
                    font-size:28px;font-weight:700;letter-spacing:4px;color:#1e3a8a;">
          {code}
        </div>
        <p style="color:#9ca3af;font-size:12px;margin-top:24px;line-height:1.6;">
          This code will expire in 10 minutes.<br>
          If you didn't request this, you can safely ignore this email.
        </p>
      </div>
    </div>
    """

    fire_and_forget(
        send_email_async(to=normalized_email, subject=subject, body_text=body_text, body_html=body_html)
    )

    return {"ok": True}


async def signup(db: AsyncIOMotorDatabase, data: SignupRequest) -> AuthResponse:
    # 1. Verify OTP
    normalized_email = data.email.lower()
    otp_key = f"otp:{normalized_email}"
    expected_code = None

    r = get_redis()
    if r is not None:
        try:
            expected_code = await r.get(otp_key)
        except Exception:
            pass

    # Fallback to local dict if not found in Redis
    if not expected_code:
        expected_code = _local_otps.get(normalized_email)

    if not expected_code or expected_code != data.otp.strip():
        raise Unauthorized("Invalid or expired verification code")

    # Clear the OTP once verified
    if r is not None:
        try:
            await r.delete(otp_key)
        except Exception:
            pass
    _local_otps.pop(normalized_email, None)

    # Derive organization name from email if company field was left blank
    org_name = data.org_name.strip() if data.org_name.strip() else data.email.split("@")[0].capitalize()

    org_doc = OrgModel.new(name=org_name, plan_type=PlanType.free)
    try:
        await db[OrgModel.COLLECTION].insert_one(org_doc)
    except DuplicateKeyError as e:
        raise Conflict("Organization name already taken") from e

    user_doc = UserModel.new(
        org_id=org_doc["_id"],
        email=data.email.lower(),
        hashed_password=hash_password(data.password),
        role=UserRole.admin,
        status=UserStatus.active,
    )
    try:
        await db[UserModel.COLLECTION].insert_one(user_doc)
    except DuplicateKeyError as e:
        await db[OrgModel.COLLECTION].delete_one({"_id": org_doc["_id"]})
        raise Conflict("A user with that email already exists in this org") from e

    # Link organization owner
    await db[OrgModel.COLLECTION].update_one(
        {"_id": org_doc["_id"]},
        {"$set": {"owner_id": user_doc["_id"]}}
    )
    org_doc["owner_id"] = user_doc["_id"]

    return _build_auth(user_doc, org_doc)


async def login(db: AsyncIOMotorDatabase, data: LoginRequest) -> AuthResponse:
    users = await db[UserModel.COLLECTION].find({"email": data.email.lower()}).to_list(length=100)

    matching_user = None
    for u in users:
        if u.get("hashed_password") and verify_password(data.password, u["hashed_password"]):
            matching_user = u
            break

    if not matching_user:
        raise Unauthorized("Invalid credentials")

    if matching_user.get("status") != UserStatus.active.value:
        raise Unauthorized("User is not active")

    org = await db[OrgModel.COLLECTION].find_one({"_id": matching_user["org_id"]})
    if not org:
        raise Unauthorized("Invalid credentials")

    return _build_auth(matching_user, org)


async def forgot_password(db: AsyncIOMotorDatabase, email: str) -> dict:
    normalized_email = email.lower()
    user = await db[UserModel.COLLECTION].find_one({
        "email": normalized_email,
        "status": UserStatus.active.value,
        "hashed_password": {"$ne": None}
    })

    if not user:
        return {"ok": True}

    org = await db[OrgModel.COLLECTION].find_one({"_id": user["org_id"]})
    if not org:
        return {"ok": True}

    token = create_reset_token(user_id=user["_id"], org_id=user["org_id"], email=user["email"])
    reset_url = f"{get_settings().APP_BASE_URL.rstrip('/')}/reset-password?token={token}"

    fire_and_forget(
        send_email_async(
            to=user["email"],
            subject="Reset your Repnex password",
            body_text=(
                f"Hi {user['email'].split('@')[0].capitalize()},\n\n"
                f"We received a request to reset your Repnex password for {org['name']}.\n\n"
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
                  <strong>{org['name']}</strong>.
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
        )
    )

    return {"ok": True}


async def reset_password(db: AsyncIOMotorDatabase, token: str, password: str) -> dict:
    payload = decode_token(token, expected_type="reset")
    jti = payload["jti"]
    r = get_redis()
    if r is not None and await r.exists(f"jwt:denylist:{jti}"):
        raise Unauthorized("Reset link already used")

    user_id = payload["sub"]
    user = await db[UserModel.COLLECTION].find_one({"_id": user_id})
    if not user or user.get("status") != UserStatus.active.value:
        raise Unauthorized("Invalid reset link")

    await db[UserModel.COLLECTION].update_one(
        {"_id": user_id},
        {"$set": {"hashed_password": hash_password(password)}}
    )

    if r is not None:
        await r.set(f"jwt:denylist:{jti}", "1", ex=60 * 60)

    return {"ok": True}


async def refresh(db: AsyncIOMotorDatabase, refresh_token: str) -> TokenPair:
    payload = decode_token(refresh_token, expected_type="refresh")
    jti = payload["jti"]
    r = get_redis()
    if r is not None and await r.exists(f"jwt:denylist:{jti}"):
        raise Unauthorized("Refresh token revoked")

    user_id = payload["sub"]
    user = await db[UserModel.COLLECTION].find_one({"_id": user_id})
    if not user or user.get("status") != UserStatus.active.value:
        raise Unauthorized("User invalid")

    if r is not None:
        await r.set(f"jwt:denylist:{jti}", "1", ex=60 * 60 * 24 * 14)

    access = create_access_token(
        user_id=user["_id"],
        org_id=user["org_id"],
        email=user["email"],
        role=user["role"],
        module_permissions=user.get("module_permissions"),
    )
    new_refresh = create_refresh_token(user_id=user["_id"], org_id=user["org_id"])
    return TokenPair(access_token=access, refresh_token=new_refresh)


async def logout(refresh_token: str) -> None:
    payload = decode_token(refresh_token, expected_type="refresh")
    r = get_redis()
    if r is not None:
        await r.set(
            f"jwt:denylist:{payload['jti']}", "1", ex=60 * 60 * 24 * 14
        )


def _build_auth(user: dict, org: dict) -> AuthResponse:
    access = create_access_token(
        user_id=user["_id"],
        org_id=user["org_id"],
        email=user["email"],
        role=user["role"],
        module_permissions=user.get("module_permissions"),
    )
    refresh_t = create_refresh_token(user_id=user["_id"], org_id=user["org_id"])

    # Extract name from email
    email_name = user["email"].split("@")[0].capitalize()

    user_public = UserPublic(
        id=user["_id"],
        org_id=user["org_id"],
        email=user["email"],
        role=user["role"],
        status=user["status"],
        name=email_name,
        company=org["name"],
        organizationId=org["_id"],
        organizationName=org["name"],
        onboardingCompleted=True,
        module_permissions=user.get("module_permissions"),
    )

    org_public = OrgPublic(
        id=org["_id"],
        name=org["name"],
        plan_type=org["plan_type"],
    )

    return AuthResponse(
        tokens=TokenPair(access_token=access, refresh_token=refresh_t),
        user=user_public,
        org=org_public,
        token=access,
    )
