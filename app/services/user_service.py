from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.models import User, UserRole
from app.core.exceptions import Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.core.security.passwords import hash_password, verify_password
from app.schemas.user import RoleUpdateRequest, UserRead


async def get_me(db: AsyncSession, current: CurrentUser) -> UserRead:
    user = (
        await db.execute(select(User).where(User.id == current.user_id))
    ).scalar_one_or_none()
    if not user:
        raise NotFound("User not found")
    return UserRead.model_validate(user)


async def list_org_users(db: AsyncSession, current: CurrentUser) -> list[UserRead]:
    rows = (
        await db.execute(
            select(User).where(User.org_id == current.org_id).order_by(User.created_at.desc())
        )
    ).scalars().all()
    return [UserRead.model_validate(u) for u in rows]


async def update_role(
    db: AsyncSession, current: CurrentUser, user_id: uuid.UUID, data: RoleUpdateRequest
) -> UserRead:
    if current.role != "admin":
        raise Forbidden("Only admins can change roles")
    user = (
        await db.execute(
            select(User).where(User.id == user_id, User.org_id == current.org_id)
        )
    ).scalar_one_or_none()
    if not user:
        raise NotFound("User not found")
    user.role = UserRole(data.role)
    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


async def change_password(
    db: AsyncSession, current: CurrentUser, current_password: str, new_password: str
) -> None:
    user = (
        await db.execute(select(User).where(User.id == current.user_id))
    ).scalar_one_or_none()
    if not user or not user.hashed_password or not verify_password(
        current_password, user.hashed_password
    ):
        raise Forbidden("Current password is incorrect")
    user.hashed_password = hash_password(new_password)
    await db.commit()
