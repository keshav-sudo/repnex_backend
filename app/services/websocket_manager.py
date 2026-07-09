from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from fastapi import WebSocket

log = get_logger(__name__)


def _default_event() -> asyncio.Event:
    ev = asyncio.Event()
    ev.set()
    return ev


@dataclass(slots=True)
class _Entry:
    websocket: WebSocket
    user_id: uuid.UUID
    org_id: uuid.UUID
    session_id: uuid.UUID
    task: asyncio.Task | None = field(default=None)
    pause_event: asyncio.Event = field(default_factory=_default_event)


class WebSocketManager:
    """In-process registry. Public interface is intentionally small so it can
    be reimplemented over Redis pub/sub without changing callers."""

    MAX_CONNECTIONS_PER_SESSION = 5   # prevent runaway connections per session
    WARN_TOTAL_CONNECTIONS = 500      # log warning when total connections exceed this

    def __init__(self) -> None:
        self._by_session: dict[uuid.UUID, list[_Entry]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._total_connections = 0

    @property
    def total_connections(self) -> int:
        return self._total_connections

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
            session_entries = self._by_session[session_id]
            # Evict oldest connections if session limit exceeded
            while len(session_entries) >= self.MAX_CONNECTIONS_PER_SESSION:
                old = session_entries.pop(0)
                try:
                    await old.websocket.close(code=1008, reason="Too many connections")
                except Exception:
                    pass
                if old.task and not old.task.done():
                    old.task.cancel()
                self._total_connections -= 1
                log.warning("ws_session_limit_evict", extra={"session_id": str(session_id)})
            session_entries.append(entry)
            self._total_connections += 1
            if self._total_connections >= self.WARN_TOTAL_CONNECTIONS:
                log.warning(
                    "ws_high_connection_count",
                    extra={"total": self._total_connections, "threshold": self.WARN_TOTAL_CONNECTIONS},
                )
        return entry

    async def disconnect(self, entry: _Entry) -> None:
        async with self._lock:
            lst = self._by_session.get(entry.session_id)
            if lst and entry in lst:
                lst.remove(entry)
                self._total_connections -= 1
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
