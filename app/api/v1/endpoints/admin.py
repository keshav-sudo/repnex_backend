from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.auth import require_role
from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.database.models import QueryHistory, User
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.query import QueryHistoryRead
from app.schemas.user import UserRead

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(bind_tenant_context), Depends(require_role("admin"))],
)


@router.get("/users", response_model=list[UserRead])
async def list_users(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> list[UserRead]:
    rows = (
        await db.execute(
            select(User).where(User.org_id == current.org_id).order_by(User.created_at.desc())
        )
    ).scalars().all()
    return [UserRead.model_validate(u) for u in rows]


@router.get("/query-history", response_model=list[QueryHistoryRead])
async def query_history(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
) -> list[QueryHistoryRead]:
    rows = (
        await db.execute(
            select(QueryHistory)
            .join(User, User.id == QueryHistory.user_id)
            .where(User.org_id == current.org_id)
            .order_by(QueryHistory.created_at.desc())
            .limit(min(limit, 500))
        )
    ).scalars().all()
    return [QueryHistoryRead.model_validate(h) for h in rows]
