from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies.auth import require_role
from app.api.dependencies.tenancy import bind_tenant_context
from app.core.database.models import Organization, QueryHistory, User
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
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[UserRead]:
    cursor = db[User.COLLECTION].find({"org_id": str(current.org_id)})
    rows = await cursor.sort("created_at", -1).to_list(length=1000)
    return [UserRead.model_validate(User(**u)) for u in rows]


async def is_sql_hidden(db: AsyncIOMotorDatabase, org_id: uuid.UUID) -> bool:
    org = await db[Organization.COLLECTION].find_one({"_id": str(org_id)})
    return bool(org.get("hide_sql_queries") if org else False)


@router.get("/query-history", response_model=list[QueryHistoryRead])
async def query_history(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    limit: int = 100,
) -> list[QueryHistoryRead]:
    cursor = db[QueryHistory.COLLECTION].find({"org_id": str(current.org_id)})
    rows = await cursor.sort("created_at", -1).limit(min(limit, 500)).to_list(length=min(limit, 500))

    hide = await is_sql_hidden(db, current.org_id)
    result = []
    for h in rows:
        val = QueryHistoryRead.model_validate(QueryHistory(**h))
        if hide:
            val.generated_sql = None
        result.append(val)
    return result
