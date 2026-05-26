from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class _Entry:
    websocket: WebSocket
    user_id: uuid.UUID
    org_id: uuid.UUID
    session_id: uuid.UUID
    task: asyncio.Task | None = field(default=None)


class WebSocketManager:
    """In-process registry. Public interface is intentionally small so it can
    be reimplemented over Redis pub/sub without changing callers."""

    def __init__(self) -> None:
        self._by_session: dict[uuid.UUID, list[_Entry]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def connect(
        self,
        websocket: WebSocket,
        *,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> _Entry:
        await websocket.accept()
        entry = _Entry(websocket=websocket, user_id=user_id, org_id=org_id, session_id=session_id)
        async with self._lock:
            self._by_session[session_id].append(entry)
        return entry

    async def disconnect(self, entry: _Entry) -> None:
        async with self._lock:
            lst = self._by_session.get(entry.session_id)
            if lst and entry in lst:
                lst.remove(entry)
                if not lst:
                    self._by_session.pop(entry.session_id, None)
        if entry.task and not entry.task.done():
            entry.task.cancel()

    async def send(self, entry: _Entry, message: dict[str, Any]) -> None:
        try:
            await entry.websocket.send_json(message)
        except Exception:
            log.warning("ws_send_failed", extra={"session_id": str(entry.session_id)})
            raise

    async def shutdown(self) -> None:
        async with self._lock:
            entries = [e for lst in self._by_session.values() for e in lst]
            self._by_session.clear()
        for e in entries:
            try:
                await e.websocket.close(code=1001)
            except Exception:
                pass
            if e.task and not e.task.done():
                e.task.cancel()


_manager: WebSocketManager | None = None


def init_ws_manager() -> WebSocketManager:
    global _manager
    _manager = WebSocketManager()
    return _manager


def get_ws_manager() -> WebSocketManager:
    if _manager is None:
        raise RuntimeError("WebSocketManager not initialized")
    return _manager


async def shutdown_ws_manager() -> None:
    global _manager
    if _manager is not None:
        await _manager.shutdown()
        _manager = None
