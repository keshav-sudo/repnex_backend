from __future__ import annotations

from fastapi import Depends, Header

from app.core.exceptions import Forbidden, Unauthorized
from app.core.security.auth import (
    CurrentUser,
    current_user_from_payload,
    decode_token,
)


async def get_current_user(
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token, expected_type="access")
    return current_user_from_payload(payload)


def require_role(*roles: str):
    async def _dep(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role not in roles:
            raise Forbidden(f"Requires role(s): {', '.join(roles)}")
        return current

    return _dep
