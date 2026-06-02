from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies.rate_limit import consume_ws_msg
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
from app.services import query_service
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

    async def send(msg: dict[str, Any]) -> None:
        await mgr.send(entry, msg)

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
    db: AsyncSession = await anext(db_iter)  # type: ignore[arg-type]
    try:
        result = await query_service.run_streaming(
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
    except AppError:
        # Cannot accept if authentication fails
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
        await gateway_mgr.unregister(current.org_id, agent_name)
