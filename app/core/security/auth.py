from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.core.config import get_settings
from app.core.exceptions import Unauthorized
from jose import JWTError, jwt

TokenType = Literal["access", "refresh", "invite", "reset"]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user_id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    module_permissions: dict[str, bool] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _encode(payload: dict[str, Any], ttl: timedelta) -> str:
    settings = get_settings()
    now = _now()
    body: dict[str, Any] = {
        **payload,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(body, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(*, user_id: uuid.UUID, org_id: uuid.UUID, email: str, role: str, module_permissions: dict[str, bool] | None = None) -> str:
    s = get_settings()
    return _encode(
        {"sub": str(user_id), "org": str(org_id), "email": email, "role": role, "perms": module_permissions, "type": "access"},
        timedelta(minutes=s.JWT_ACCESS_TTL_MIN),
    )


def create_refresh_token(*, user_id: uuid.UUID, org_id: uuid.UUID) -> str:
    s = get_settings()
    return _encode(
        {"sub": str(user_id), "org": str(org_id), "type": "refresh"},
        timedelta(days=s.JWT_REFRESH_TTL_DAYS),
    )


def create_invite_token(*, user_id: uuid.UUID, org_id: uuid.UUID) -> str:
    s = get_settings()
    return _encode(
        {"sub": str(user_id), "org": str(org_id), "type": "invite"},
        timedelta(hours=s.INVITE_TTL_HOURS),
    )


def create_reset_token(*, user_id: uuid.UUID, org_id: uuid.UUID, email: str) -> str:
    s = get_settings()
    return _encode(
        {"sub": str(user_id), "org": str(org_id), "email": email, "type": "reset"},
        timedelta(minutes=s.PASSWORD_RESET_TTL_MIN),
    )


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as e:
        raise Unauthorized("Invalid or expired token") from e
    if payload.get("type") != expected_type:
        raise Unauthorized("Wrong token type")
    return payload


def current_user_from_payload(payload: dict[str, Any]) -> CurrentUser:
    try:
        return CurrentUser(
            user_id=uuid.UUID(payload["sub"]),
            org_id=uuid.UUID(payload["org"]),
            email=payload.get("email", ""),
            role=payload.get("role", "viewer"),
            module_permissions=payload.get("perms"),
        )
    except (KeyError, ValueError) as e:
        raise Unauthorized("Malformed token") from e
