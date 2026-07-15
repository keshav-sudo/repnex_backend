from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.database.models import DBConnection, GISession, SessionStatus
from app.core.exceptions import NotFound
from app.core.security.auth import CurrentUser
from app.schemas.session import SessionCreate, SessionDetail, SessionRead, SessionUpdate
from app.services import connection_service
from motor.motor_asyncio import AsyncIOMotorDatabase

MAX_CONTEXT_TURNS = 20


async def list_sessions(db: AsyncIOMotorDatabase, current: CurrentUser) -> list[SessionRead]:
    cursor = db[GISession.COLLECTION].find({
        "org_id": str(current.org_id),
        "user_id": str(current.user_id)
    })
    rows = await cursor.sort("created_at", -1).to_list(length=1000)
    return [SessionRead.model_validate(GISession(**r)) for r in rows]


async def get(db: AsyncIOMotorDatabase, current: CurrentUser, session_id: uuid.UUID) -> GISession:
    s = await db[GISession.COLLECTION].find_one({
        "_id": str(session_id),
        "org_id": str(current.org_id),
        "user_id": str(current.user_id),
    })
    if not s:
        raise NotFound("Session not found")
    return GISession(**s)


async def get_detail(
    db: AsyncIOMotorDatabase, current: CurrentUser, session_id: uuid.UUID
) -> SessionDetail:
    s = await get(db, current, session_id)
    return SessionDetail.model_validate(s)


async def create(
    db: AsyncIOMotorDatabase, current: CurrentUser, data: SessionCreate
) -> SessionRead:
    conn_id = data.connection_id

    # Auto-resolve connection if not provided
    if conn_id is None:
        first_conn = await db[DBConnection.COLLECTION].find_one(
            {"org_id": str(current.org_id)}
        )
        if first_conn is None:
            raise NotFound("No database connection found. Please add a connection first.")
        conn_id = uuid.UUID(first_conn["_id"])
    else:
        # Verifies access when connection_id is explicitly provided
        await connection_service.get_connection(db, current, conn_id)

    title = data.title or f"New chat {datetime.now(UTC):%Y-%m-%d %H:%M}"
    s_doc = GISession.new(
        user_id=str(current.user_id),
        org_id=str(current.org_id),
        connection_id=str(conn_id),
        title=title,
        context_window=[],
        token_count=0,
        status=SessionStatus.active,
    )
    await db[GISession.COLLECTION].insert_one(s_doc)
    return SessionRead.model_validate(GISession(**s_doc))


async def update(
    db: AsyncIOMotorDatabase, current: CurrentUser, session_id: uuid.UUID, data: SessionUpdate
) -> SessionRead:
    s = await get(db, current, session_id)
    update_fields = {}
    if data.title is not None:
        update_fields["title"] = data.title
    if data.status is not None:
        update_fields["status"] = SessionStatus(data.status).value

    if update_fields:
        await db[GISession.COLLECTION].update_one(
            {"_id": str(session_id)},
            {"$set": update_fields}
        )
        # Fetch updated
        updated_doc = await db[GISession.COLLECTION].find_one({"_id": str(session_id)})
        s = GISession(**updated_doc)

    return SessionRead.model_validate(s)


async def archive(
    db: AsyncIOMotorDatabase, current: CurrentUser, session_id: uuid.UUID
) -> SessionRead:
    return await update(
        db, current, session_id, SessionUpdate(status="archived")
    )


async def delete(
    db: AsyncIOMotorDatabase, current: CurrentUser, session_id: uuid.UUID
) -> None:
    s = await get(db, current, session_id)
    await db[GISession.COLLECTION].delete_one({"_id": str(session_id)})


from typing import Any


def make_json_safe(obj: Any) -> Any:
    import decimal
    import uuid
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict) or hasattr(obj, "items"):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list) or isinstance(obj, (tuple, set)):
        return [make_json_safe(x) for x in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
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
    db: AsyncIOMotorDatabase, session: GISession, *, role: str, content: str, **kwargs
) -> None:
    cw = list(session.context_window or [])
    turn = {
        "role": role,
        "content": content,
        "timestamp": datetime.now(UTC).isoformat()
    }
    if kwargs:
        safe_kwargs = make_json_safe(kwargs)
        turn.update(safe_kwargs)
    cw.append(turn)
    if len(cw) > MAX_CONTEXT_TURNS:
        cw = cw[-MAX_CONTEXT_TURNS:]

    token_count = sum(len(m.get("content", "")) // 4 for m in cw)

    session.context_window = cw
    session.token_count = token_count

    await db[GISession.COLLECTION].update_one(
        {"_id": str(session.id)},
        {"$set": {
            "context_window": cw,
            "token_count": token_count
        }}
    )


async def edit_turn(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    session_id: uuid.UUID,
    turn_index: int,
) -> GISession:
    session = await get(db, current, session_id)
    cw = list(session.context_window or [])
    if turn_index < 0:
        raise NotFound("Turn index out of bounds")

    if turn_index >= len(cw):
        return session

    # Truncate starting from turn_index
    cw = cw[:turn_index]

    token_count = sum(len(m.get("content", "")) // 4 for m in cw)
    session.context_window = cw
    session.token_count = token_count

    await db[GISession.COLLECTION].update_one(
        {"_id": str(session.id)},
        {"$set": {
            "context_window": cw,
            "token_count": token_count
        }}
    )
    return session
