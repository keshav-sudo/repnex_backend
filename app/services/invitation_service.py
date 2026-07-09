from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.core.database.models import Organization, User, UserRole, UserStatus
from app.core.exceptions import Conflict, Forbidden, NotFound
from app.core.logging import get_logger
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
    InvitePreview,
    OrgPublic,
    TokenPair,
    UserPublic,
)
from app.schemas.user import InviteRequest, InviteResponse
from app.utils.email import fire_and_forget, send_email_async
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

log = get_logger(__name__)


async def invite(
    db: AsyncIOMotorDatabase, *, current_user_id: uuid.UUID, current_org_id: uuid.UUID,
    current_role: str, data: InviteRequest,
) -> InviteResponse:
    if current_role != "admin":
        raise Forbidden("Only admins may invite")

    existing = await db[User.COLLECTION].find_one({
        "org_id": str(current_org_id),
        "email": data.email.lower(),
    })

    if existing:
        if existing.get("status") == UserStatus.active.value:
            raise Conflict("A user with that email is already an active member of this org")
        user_id_str = existing["_id"]
        user_status = existing["status"]
    else:
        user_doc = User.new(
            org_id=str(current_org_id),
            email=data.email.lower(),
            hashed_password=None,
            role=UserRole(data.role),
            status=UserStatus.pending,
            invited_by=str(current_user_id),
        )
        try:
            await db[User.COLLECTION].insert_one(user_doc)
        except DuplicateKeyError as e:
            raise Conflict("A user with that email already exists in this org") from e
        user_id_str = user_doc["_id"]
        user_status = user_doc["status"]

    token = create_invite_token(user_id=uuid.UUID(user_id_str), org_id=current_org_id)
    org = await db[Organization.COLLECTION].find_one({"_id": str(current_org_id)})
    if not org:
        raise NotFound("Organization not found")

    settings = get_settings()
    accept_url = f"{settings.APP_BASE_URL}/accept-invite?token={token}"

    print(f"\n--- [INVITE DEBUG] Invitation URL for {data.email.lower()} is: {accept_url} ---\n", flush=True)

    fire_and_forget(
        _send_invite_async(to=data.email.lower(), accept_url=accept_url, org_name=org["name"])
    )

    return InviteResponse(user_id=uuid.UUID(user_id_str), status=user_status, accept_url=accept_url)


async def preview(db: AsyncIOMotorDatabase, token: str) -> InvitePreview:
    payload = decode_token(token, expected_type="invite")
    user_id = payload["sub"]
    user = await db[User.COLLECTION].find_one({"_id": user_id})
    if not user:
        raise NotFound("Invitation invalid or user not found")

    org = await db[Organization.COLLECTION].find_one({"_id": user["org_id"]})
    if not org:
        raise NotFound("Organization not found")

    return InvitePreview(
        email=user["email"],
        organization_name=org["name"],
        role=user["role"],
        status=user["status"],
    )


async def _send_invite_async(*, to: str, accept_url: str, org_name: str) -> None:
    body_text = (
        f"You have been invited to join {org_name} on Repnex.\n\n"
        f"Accept your invite here: {accept_url}\n\n"
        f"This link expires in 24 hours."
    )
    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin:0;padding:0;font-family:'Segoe UI',Roboto,sans-serif;background:#f4f4f7;">
      <div style="max-width:520px;margin:40px auto;background:#ffffff;border-radius:16px;
                  box-shadow:0 4px 24px rgba(0,0,0,0.08);overflow:hidden;">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:32px 24px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;letter-spacing:-0.3px;">
            Repnex
          </h1>
          <p style="margin:6px 0 0;color:rgba(255,255,255,0.85);font-size:13px;">
            AI-Powered ERP Intelligence
          </p>
        </div>
        <!-- Body -->
        <div style="padding:32px 24px;">
          <h2 style="margin:0 0 8px;color:#111827;font-size:18px;font-weight:600;">
            You&rsquo;re invited! 🎉
          </h2>
          <p style="margin:0 0 20px;color:#6b7280;font-size:14px;line-height:1.6;">
            <strong>{org_name}</strong> has invited you to collaborate on Repnex &mdash;
            your AI-powered ERP reporting platform. Connect your databases, ask questions
            in plain English, and get instant insights.
          </p>
          <!-- CTA Button -->
          <div style="text-align:center;margin:28px 0;">
            <a href="{accept_url}"
               style="display:inline-block;background:linear-gradient(135deg,#2563eb,#1d4ed8);
                      color:#ffffff;text-decoration:none;padding:14px 36px;border-radius:12px;
                      font-size:15px;font-weight:600;letter-spacing:0.3px;
                      box-shadow:0 4px 14px rgba(37,99,235,0.35);">
              Accept Invitation &amp; Set Password
            </a>
          </div>
          <p style="margin:24px 0 0;color:#9ca3af;font-size:12px;line-height:1.5;text-align:center;">
            This invitation link expires in <strong>24 hours</strong>.<br>
            If you didn&rsquo;t expect this, you can safely ignore this email.
          </p>
        </div>
        <!-- Footer -->
        <div style="background:#f9fafb;padding:16px 24px;text-align:center;
                    border-top:1px solid #e5e7eb;">
          <p style="margin:0;color:#9ca3af;font-size:11px;">
            &copy; Repnex &bull; AI-Powered ERP Reports
          </p>
        </div>
      </div>
    </body>
    </html>
    """
    try:
        await send_email_async(
            to=to,
            subject=f"🎉 You're invited to join {org_name} on Repnex",
            body_text=body_text,
            body_html=body_html,
        )
    except Exception as e:
        log.exception("invite_email_send_failed", extra={"to": to, "error": str(e)})


async def accept(db: AsyncIOMotorDatabase, data: AcceptInviteRequest) -> AuthResponse:
    settings = get_settings()
    dashboard_url = f"{settings.APP_BASE_URL.rstrip('/')}/dashboard"

    payload = decode_token(data.token, expected_type="invite")
    user_id = payload["sub"]
    user = await db[User.COLLECTION].find_one({"_id": user_id})
    if not user:
        raise NotFound("Invitation invalid or user not found")
    if user.get("status") not in (UserStatus.pending.value, UserStatus.expired.value):
        raise Conflict("This invitation has already been accepted")

    hashed_pw = hash_password(data.password)
    await db[User.COLLECTION].update_one(
        {"_id": user_id},
        {"$set": {
            "hashed_password": hashed_pw,
            "status": UserStatus.active.value
        }}
    )
    user["status"] = UserStatus.active.value
    user["hashed_password"] = hashed_pw

    org = await db[Organization.COLLECTION].find_one({"_id": user["org_id"]})
    if not org:
        raise NotFound("Organization not found")

    email_name = user["email"].split("@")[0].capitalize()
    access = create_access_token(
        user_id=uuid.UUID(user["_id"]),
        org_id=uuid.UUID(user["org_id"]),
        email=user["email"],
        role=user["role"]
    )
    refresh_t = create_refresh_token(
        user_id=uuid.UUID(user["_id"]),
        org_id=uuid.UUID(user["org_id"])
    )

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
    )

    org_public = OrgPublic(
        id=org["_id"],
        name=org["name"],
        plan_type=org["plan_type"],
    )

    fire_and_forget(
        send_email_async(
            to=user["email"],
            subject=f"✅ You're now a member of {org['name']} on Repnex",
            body_text=(
                f"Hi {email_name},\n\n"
                f"Your account on Repnex has been activated. You are now a member of {org['name']}.\n\n"
                f"Get started: {dashboard_url}\n\n"
                f"— The Repnex Team"
            ),
            body_html=f"""
            <div style="font-family:'Segoe UI',sans-serif;max-width:520px;margin:40px auto;
                        background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.08);overflow:hidden;">
              <div style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:32px 24px;text-align:center;">
                <h1 style="margin:0;color:#fff;font-size:22px;">Welcome to Repnex! ✅</h1>
              </div>
              <div style="padding:32px 24px;">
                <p style="color:#374151;font-size:15px;line-height:1.6;">
                  Hi <strong>{email_name}</strong>,
                </p>
                <p style="color:#6b7280;font-size:14px;line-height:1.6;">
                  Your account has been activated and you are now a member of
                  <strong>{org['name']}</strong> on Repnex.
                </p>
                <div style="text-align:center;margin:28px 0;">
                  <a href="{dashboard_url}"
                     style="display:inline-block;background:linear-gradient(135deg,#2563eb,#1d4ed8);
                            color:#fff;text-decoration:none;padding:14px 36px;border-radius:12px;
                            font-size:15px;font-weight:600;">
                    Go to Dashboard →
                  </a>
                </div>
              </div>
            </div>
            """,
        )
    )

    return AuthResponse(
        tokens=TokenPair(access_token=access, refresh_token=refresh_t),
        user=user_public,
        org=org_public,
        token=access,
    )
