from __future__ import annotations

import uuid
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database.models import User, UserRole
from app.core.exceptions import Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.core.security.passwords import hash_password, verify_password
from app.schemas.user import RoleUpdateRequest, UserRead, PermissionsUpdateRequest


async def get_me(db: AsyncIOMotorDatabase, current: CurrentUser) -> UserRead:
    user = await db[User.COLLECTION].find_one({"_id": str(current.user_id)})
    if not user:
        raise NotFound("User not found")
    return UserRead.model_validate(User(**user))


async def list_org_users(db: AsyncIOMotorDatabase, current: CurrentUser) -> list[UserRead]:
    cursor = db[User.COLLECTION].find({"org_id": str(current.org_id)})
    rows = await cursor.sort("created_at", -1).to_list(length=1000)
    return [UserRead.model_validate(User(**u)) for u in rows]


async def update_role(
    db: AsyncIOMotorDatabase, current: CurrentUser, user_id: uuid.UUID, data: RoleUpdateRequest
) -> UserRead:
    if current.role != "admin":
        raise Forbidden("Only admins can change roles")

    user = await db[User.COLLECTION].find_one({
        "_id": str(user_id),
        "org_id": str(current.org_id)
    })
    if not user:
        raise NotFound("User not found")

    await db[User.COLLECTION].update_one(
        {"_id": str(user_id)},
        {"$set": {"role": UserRole(data.role).value}}
    )

    updated_doc = await db[User.COLLECTION].find_one({"_id": str(user_id)})
    return UserRead.model_validate(User(**updated_doc))


async def update_permissions(
    db: AsyncIOMotorDatabase, current: CurrentUser, user_id: uuid.UUID, data: PermissionsUpdateRequest
) -> UserRead:
    if current.role != "admin":
        raise Forbidden("Only admins can manage permissions")

    user = await db[User.COLLECTION].find_one({
        "_id": str(user_id),
        "org_id": str(current.org_id)
    })
    if not user:
        raise NotFound("User not found")

    current_perms = dict(user.get("module_permissions") or {})
    current_perms.update(data.module_permissions)

    await db[User.COLLECTION].update_one(
        {"_id": str(user_id)},
        {"$set": {"module_permissions": current_perms}}
    )

    updated_doc = await db[User.COLLECTION].find_one({"_id": str(user_id)})
    return UserRead.model_validate(User(**updated_doc))


async def change_password(
    db: AsyncIOMotorDatabase, current: CurrentUser, current_password: str, new_password: str
) -> None:
    user = await db[User.COLLECTION].find_one({"_id": str(current.user_id)})
    if not user or not user.get("hashed_password") or not verify_password(
        current_password, user["hashed_password"]
    ):
        raise Forbidden("Current password is incorrect")

    await db[User.COLLECTION].update_one(
        {"_id": str(current.user_id)},
        {"$set": {"hashed_password": hash_password(new_password)}}
    )
