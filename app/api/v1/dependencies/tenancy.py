from __future__ import annotations

from fastapi import Depends

from app.api.v1.dependencies.auth import get_current_user
from app.core.logging import org_id_ctx, user_id_ctx
from app.core.security.auth import CurrentUser


async def bind_tenant_context(
    current: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    org_id_ctx.set(str(current.org_id))
    user_id_ctx.set(str(current.user_id))
    return current
