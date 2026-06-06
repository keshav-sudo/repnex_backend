from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.models import DBConnection, GISession, SessionStatus
from app.core.exceptions import NotFound
from app.core.security.auth import CurrentUser
from app.schemas.session import SessionCreate, SessionDetail, SessionRead, SessionUpdate
from app.services import connection_service

MAX_CONTEXT_TURNS = 20


async def list_sessions(db: AsyncSession, current: CurrentUser) -> list[SessionRead]:
    rows = (
        await db.execute(
            select(GISession)
            .where(GISession.org_id == current.org_id, GISession.user_id == current.user_id)
            .order_by(GISession.created_at.desc())
        )
    ).scalars().all()
    return [SessionRead.model_validate(r) for r in rows]


async def get(db: AsyncSession, current: CurrentUser, session_id: uuid.UUID) -> GISession:
    s = (
        await db.execute(
            select(GISession).where(
                GISession.id == session_id,
                GISession.org_id == current.org_id,
                GISession.user_id == current.user_id,
            )
        )
    ).scalar_one_or_none()
    if not s:
        raise NotFound("Session not found")
    return s


async def get_detail(
    db: AsyncSession, current: CurrentUser, session_id: uuid.UUID
) -> SessionDetail:
    s = await get(db, current, session_id)
    return SessionDetail.model_validate(s)


async def create(
    db: AsyncSession, current: CurrentUser, data: SessionCreate
) -> SessionRead:
    conn_id = data.connection_id

    # Auto-resolve connection if not provided
    if conn_id is None:
        first_conn = (
            await db.execute(
                select(DBConnection)
                .where(DBConnection.org_id == current.org_id)
                .order_by(DBConnection.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if first_conn is None:
            raise NotFound("No database connection found. Please add a connection first.")
        conn_id = first_conn.id
    else:
        # Verifies access when connection_id is explicitly provided
        await connection_service.get_connection(db, current, conn_id)

    title = data.title or f"New chat {datetime.now(timezone.utc):%Y-%m-%d %H:%M}"
    s = GISession(
        user_id=current.user_id,
        org_id=current.org_id,
        connection_id=conn_id,
        title=title,
        context_window=[],
        token_count=0,
        status=SessionStatus.active,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return SessionRead.model_validate(s)


async def update(
    db: AsyncSession, current: CurrentUser, session_id: uuid.UUID, data: SessionUpdate
) -> SessionRead:
    s = await get(db, current, session_id)
    if data.title is not None:
        s.title = data.title
    if data.status is not None:
        s.status = SessionStatus(data.status)
    await db.commit()
    await db.refresh(s)
    return SessionRead.model_validate(s)


async def archive(
    db: AsyncSession, current: CurrentUser, session_id: uuid.UUID
) -> SessionRead:
    return await update(
        db, current, session_id, SessionUpdate(status="archived")
    )


async def delete(
    db: AsyncSession, current: CurrentUser, session_id: uuid.UUID
) -> None:
    s = await get(db, current, session_id)
    await db.delete(s)
    await db.commit()


from typing import Any

def make_json_safe(obj: Any) -> Any:
    import decimal
    import uuid
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_safe(x) for x in obj]
    elif isinstance(obj, (tuple, set)):
        return [make_json_safe(x) for x in obj]
    elif hasattr(obj, "isoformat"):
        return obj.isoformat()
    elif isinstance(obj, decimal.Decimal):
        try:
            return float(obj)
        except Exception:
            return str(obj)
    elif isinstance(obj, uuid.UUID):
        return str(obj)
    elif isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


async def append_turn(
    db: AsyncSession, session: GISession, *, role: str, content: str, **kwargs
) -> None:
    cw = list(session.context_window or [])
    turn = {"role": role, "content": content}
    if kwargs:
        safe_kwargs = make_json_safe(kwargs)
        turn.update(safe_kwargs)
    cw.append(turn)
    if len(cw) > MAX_CONTEXT_TURNS:
        cw = cw[-MAX_CONTEXT_TURNS:]
    session.context_window = cw
    session.token_count = sum(len(m.get("content", "")) // 4 for m in cw)
    await db.commit()
