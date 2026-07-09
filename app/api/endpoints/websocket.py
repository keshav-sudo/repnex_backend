from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import TypeAdapter, ValidationError

from app.api.dependencies.rate_limit import consume_ws_msg
from app.core.database.session import get_db
from app.core.exceptions import AppError, RateLimited
from app.core.logging import get_logger, org_id_ctx, user_id_ctx
from app.core.security.auth import (
    current_user_from_payload,
    decode_token,
)
from app.schemas.websocket import (
    CompleteMsg,
    ErrorMsg,
    PongMsg,
    ReadyMsg,
    WSClientMessage,
)
from app.services.chat import run_streaming
from app.services.websocket_manager import get_ws_manager

router = APIRouter(tags=["websocket"])
log = get_logger(__name__)

_client_msg_adapter: TypeAdapter[WSClientMessage] = TypeAdapter(WSClientMessage)


@router.websocket("/ws/query/{session_id}")
async def ws_query(
    websocket: WebSocket,
    session_id: uuid.UUID,
    token: str = Query(...),
) -> None:
    try:
        payload = decode_token(token, expected_type="access")
        current = current_user_from_payload(payload)
    except AppError:
        await websocket.close(code=1008)
        return

    org_id_ctx.set(str(current.org_id))
    user_id_ctx.set(str(current.user_id))

    mgr = get_ws_manager()
    entry = await mgr.connect(
        websocket, user_id=current.user_id, org_id=current.org_id, session_id=session_id
    )

    db_iter = get_db()
    db: AsyncIOMotorDatabase = await anext(db_iter)  # type: ignore[arg-type]
    from app.core.database.models import Organization
    org = await db[Organization.COLLECTION].find_one({"_id": str(current.org_id)})
    hide_sql = bool(org.get("hide_sql_queries") if org else False)

    def redact_sql_blocks(text: str | None) -> str | None:
        import re
        if not text:
            return text
        return re.sub(
            r"(```sql\s+)(.*?)(```)",
            r"\1-- SQL hidden by organization settings\n\3",
            text,
            flags=re.DOTALL | re.IGNORECASE
        )

    def apply_ws_redaction(msg: dict[str, Any]) -> dict[str, Any] | None:
        if not hide_sql:
            return msg
        if msg.get("type") == "sql":
            return None
        if "sql" in msg:
            msg["sql"] = None
        if "message" in msg and isinstance(msg["message"], str):
            cleaned = msg["message"]
            cleaned = cleaned.replace(" Here's a preview of the SQL that will execute:", "")
            cleaned = cleaned.replace("Here's a preview of the SQL that will execute:", "")
            msg["message"] = redact_sql_blocks(cleaned)
        if "summary" in msg and isinstance(msg["summary"], str):
            msg["summary"] = redact_sql_blocks(msg["summary"])
        return msg

    async def send(msg: dict[str, Any]) -> None:
        redacted = apply_ws_redaction(msg)
        if redacted is not None:
            await mgr.send(entry, redacted)

    try:
        await send(ReadyMsg(session_id=str(session_id)).model_dump())

        while True:
            raw = await websocket.receive_json()
            try:
                msg = _client_msg_adapter.validate_python(raw)
            except ValidationError as e:
                await send(ErrorMsg(code="invalid_message", message=str(e.errors()[:1])).model_dump())
                continue

            try:
                await consume_ws_msg(str(current.user_id))
            except RateLimited as e:
                await send(ErrorMsg(code="rate_limited", message=e.message).model_dump())
                continue

            if msg.action == "ping":
                await send(PongMsg().model_dump())
                continue

            if msg.action == "cancel":
                if entry.task and not entry.task.done():
                    entry.task.cancel()
                continue

            if msg.action == "run_query":
                if entry.task and not entry.task.done():
                    await send(
                        ErrorMsg(code="busy", message="Query already running").model_dump()
                    )
                    continue
                entry.task = asyncio.create_task(
                    _run(send, current, session_id, msg.natural_language),
                    name=f"ws-run-{session_id}",
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws_unhandled")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await mgr.disconnect(entry)


async def _run(
    send,
    current,
    session_id: uuid.UUID,
    natural_language: str,
) -> None:
    db_iter = get_db()
    db: AsyncIOMotorDatabase = await anext(db_iter)  # type: ignore[arg-type]
    try:
        result = await run_streaming(
            db,
            current,
            session_id=session_id,
            natural_language=natural_language,
            on_event=send,
        )
        await send(CompleteMsg(**result).model_dump())
    except AppError as e:
        await send(ErrorMsg(code=e.code, message=e.message).model_dump())
    except asyncio.CancelledError:
        await send(ErrorMsg(code="cancelled", message="Query cancelled").model_dump())
        raise
    except Exception:
        log.exception("ws_run_unhandled")
        await send(ErrorMsg(code="internal_error", message="Server error").model_dump())
    finally:
        try:
            await anext(db_iter)  # type: ignore[arg-type]
        except StopAsyncIteration:
            pass


@router.websocket("/ws/gateway")
async def ws_gateway(
    websocket: WebSocket,
    agent_name: str = Query(...),
    token: str = Query(...),
) -> None:
    try:
        payload = decode_token(token, expected_type="access")
        current = current_user_from_payload(payload)
    except AppError as e:
        log.warning("ws_gateway_auth_failed", extra={"agent_name": agent_name, "error": str(e)})
        # Accept and immediately close with 1008 policy violation
        await websocket.accept()
        await websocket.close(code=1008, reason=f"Authentication failed: {str(e)}")
        return

    org_id_ctx.set(str(current.org_id))
    user_id_ctx.set(str(current.user_id))

    from app.services.gateway_manager import get_gateway_manager
    gateway_mgr = get_gateway_manager()

    await websocket.accept()
    await gateway_mgr.register(current.org_id, agent_name, websocket)

    try:
        while True:
            raw = await websocket.receive_json()
            if isinstance(raw, dict) and raw.get("action") == "query_response":
                gateway_mgr.handle_response(raw)
            elif isinstance(raw, dict) and raw.get("action") == "ping":
                await websocket.send_json({"action": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("gateway_ws_unhandled")
    finally:
        await gateway_mgr.unregister(current.org_id, agent_name, websocket)
