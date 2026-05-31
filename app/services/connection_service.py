from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.models import (
    DBConnection,
    DBConnectionAccess,
    DBType,
)
from app.core.database.target_pool import get_target_pool_registry
from app.core.exceptions import Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.core.security.encryption import encrypt
from app.schemas.connection import (
    AccessGrantRead,
    AccessGrantRequest,
    ConnectionCreate,
    ConnectionRead,
    ConnectionUpdate,
    TestConnectionResponse,
)


async def list_connections(
    db: AsyncSession, current: CurrentUser
) -> list[ConnectionRead]:
    base = select(DBConnection).where(DBConnection.org_id == current.org_id)
    if current.role != "admin":
        access = (
            select(DBConnectionAccess.connection_id)
            .where(DBConnectionAccess.org_id == current.org_id)
            .where(
                or_(
                    DBConnectionAccess.user_id == current.user_id,
                    DBConnectionAccess.user_id.is_(None),
                )
            )
        )
        base = base.where(DBConnection.id.in_(access))
    rows = (await db.execute(base.order_by(DBConnection.created_at.desc()))).scalars().all()
    return [ConnectionRead.model_validate(r) for r in rows]


async def get_connection(
    db: AsyncSession, current: CurrentUser, conn_id: uuid.UUID
) -> DBConnection:
    if current.role == "admin":
        # Admin: single lookup by id + org
        conn = (
            await db.execute(
                select(DBConnection).where(
                    DBConnection.id == conn_id, DBConnection.org_id == current.org_id
                )
            )
        ).scalar_one_or_none()
    else:
        # Non-admin: merge access check into the same query via EXISTS
        from sqlalchemy import exists
        access_exists = (
            select(DBConnectionAccess.id)
            .where(DBConnectionAccess.connection_id == conn_id)
            .where(DBConnectionAccess.org_id == current.org_id)
            .where(
                or_(
                    DBConnectionAccess.user_id == current.user_id,
                    DBConnectionAccess.user_id.is_(None),
                )
            )
            .limit(1)
            .correlate_except(DBConnectionAccess)
        )
        conn = (
            await db.execute(
                select(DBConnection).where(
                    DBConnection.id == conn_id,
                    DBConnection.org_id == current.org_id,
                    exists(access_exists),
                )
            )
        ).scalar_one_or_none()
    if not conn:
        raise NotFound("Connection not found")
    return conn


async def create_connection(
    db: AsyncSession, current: CurrentUser, data: ConnectionCreate
) -> ConnectionRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create connections")

    conn = DBConnection(
        org_id=current.org_id,
        created_by=current.user_id,
        name=data.name,
        db_type=DBType(data.db_type),
        host=data.host,
        port=data.port,
        db_name=data.db_name,
        encrypted_username=encrypt(data.username),
        encrypted_password=encrypt(data.password),
        ssl_enabled=data.ssl_enabled,
        is_active=True,
    )
    db.add(conn)
    await db.flush()

    db.add(
        DBConnectionAccess(
            connection_id=conn.id,
            user_id=None,  # whole org by default
            org_id=current.org_id,
            granted_by=current.user_id,
        )
    )
    await db.commit()
    await db.refresh(conn)
    return ConnectionRead.model_validate(conn)


async def update_connection(
    db: AsyncSession, current: CurrentUser, conn_id: uuid.UUID, data: ConnectionUpdate
) -> ConnectionRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot update connections")
    conn = await get_connection(db, current, conn_id)
    payload = data.model_dump(exclude_unset=True)
    if "username" in payload:
        conn.encrypted_username = encrypt(payload.pop("username"))
    if "password" in payload:
        conn.encrypted_password = encrypt(payload.pop("password"))
    for k, v in payload.items():
        setattr(conn, k, v)
    await db.commit()
    await db.refresh(conn)
    await get_target_pool_registry().evict(conn.id)
    return ConnectionRead.model_validate(conn)


async def delete_connection(
    db: AsyncSession, current: CurrentUser, conn_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete connections")
    conn = await get_connection(db, current, conn_id)
    await db.delete(conn)
    await db.commit()
    await get_target_pool_registry().evict(conn_id)


async def test_connection(
    db: AsyncSession, current: CurrentUser, conn_id: uuid.UUID
) -> TestConnectionResponse:
    conn = await get_connection(db, current, conn_id)
    started = time.perf_counter()
    try:
        pool = await get_target_pool_registry().get_pool(conn)
        await pool.execute_one("SELECT 1", {}, timeout=5.0)
    except Exception as e:
        return TestConnectionResponse(ok=False, error=e.__class__.__name__)
    conn.last_tested_at = datetime.now(timezone.utc)
    await db.commit()
    return TestConnectionResponse(
        ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
    )


async def test_raw_connection(
    current: CurrentUser, data: ConnectionCreate
) -> TestConnectionResponse:
    # Build a temporary DBConnection object to feed to pool registry
    temp_conn = DBConnection(
        id=uuid.uuid4(),
        org_id=current.org_id,
        created_by=current.user_id,
        name=data.name,
        db_type=DBType(data.db_type),
        host=data.host,
        port=data.port,
        db_name=data.db_name,
        encrypted_username=encrypt(data.username),
        encrypted_password=encrypt(data.password),
        ssl_enabled=data.ssl_enabled,
        is_active=True,
    )
    started = time.perf_counter()
    try:
        registry = get_target_pool_registry()
        pool = await registry._build(temp_conn)
        # Verify connection by executing simple scalar query
        await pool.execute_one("SELECT 1", {}, timeout=5.0)
        await pool.close()
    except Exception as e:
        return TestConnectionResponse(ok=False, error=str(e) or e.__class__.__name__)
    return TestConnectionResponse(
        ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
    )


async def grant_access(
    db: AsyncSession, current: CurrentUser, conn_id: uuid.UUID, data: AccessGrantRequest
) -> AccessGrantRead:
    if current.role != "admin":
        raise Forbidden("Only admins can grant access")
    conn = await get_connection(db, current, conn_id)
    grant = DBConnectionAccess(
        connection_id=conn.id,
        user_id=data.user_id,
        org_id=current.org_id,
        granted_by=current.user_id,
    )
    db.add(grant)
    await db.commit()
    await db.refresh(grant)
    return AccessGrantRead.model_validate(grant)


async def revoke_access(
    db: AsyncSession, current: CurrentUser, grant_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can revoke access")
    grant = (
        await db.execute(
            select(DBConnectionAccess).where(
                DBConnectionAccess.id == grant_id,
                DBConnectionAccess.org_id == current.org_id,
            )
        )
    ).scalar_one_or_none()
    if not grant:
        raise NotFound("Grant not found")
    await db.delete(grant)
    await db.commit()


async def _assert_access(
    db: AsyncSession, current: CurrentUser, conn_id: uuid.UUID
) -> None:
    if current.role == "admin":
        return
    has = (
        await db.execute(
            select(DBConnectionAccess.id)
            .where(DBConnectionAccess.connection_id == conn_id)
            .where(DBConnectionAccess.org_id == current.org_id)
            .where(
                or_(
                    DBConnectionAccess.user_id == current.user_id,
                    DBConnectionAccess.user_id.is_(None),
                )
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not has:
        raise NotFound("Connection not found")
