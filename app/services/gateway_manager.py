from __future__ import annotations

import asyncio
import uuid
from typing import Any
from fastapi import WebSocket

from app.core.logging import get_logger

log = get_logger(__name__)

class GatewayManager:
    """
    Manages active outbound WebSocket connections from local Gateway Agents.
    Routes queries from the cloud to the correct local agent.
    """

    def __init__(self) -> None:
        # Key: "org_id:agent_name" -> WebSocket
        self._agents: dict[str, WebSocket] = {}
        # Key: query_id (str) -> asyncio.Future
        self._pending_queries: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def register(self, org_id: uuid.UUID | str, agent_name: str, websocket: WebSocket) -> None:
        key = f"{org_id}:{agent_name}"
        async with self._lock:
            # If an agent with the same name was already connected, close the old one
            if key in self._agents:
                try:
                    await self._agents[key].close(code=1008, reason="Newer agent registered")
                except Exception:
                    pass
            self._agents[key] = websocket
        log.info("gateway_agent_registered", extra={"org_id": str(org_id), "agent_name": agent_name})

    async def unregister(self, org_id: uuid.UUID | str, agent_name: str) -> None:
        key = f"{org_id}:{agent_name}"
        async with self._lock:
            self._agents.pop(key, None)
        log.info("gateway_agent_unregistered", extra={"org_id": str(org_id), "agent_name": agent_name})

    def is_agent_active(self, org_id: uuid.UUID | str, agent_name: str) -> bool:
        return f"{org_id}:{agent_name}" in self._agents

    def list_active_agents(self, org_id: uuid.UUID | str) -> list[str]:
        prefix = f"{org_id}:"
        return [k.split(":")[1] for k in self._agents.keys() if k.startswith(prefix)]

    async def execute_query(
        self,
        org_id: uuid.UUID | str,
        agent_name: str,
        sql: str,
        params: dict[str, Any],
        db_name: str,
        db_type: str,
        timeout: float = 60.0,
    ) -> list[dict[str, Any]]:
        key = f"{org_id}:{agent_name}"
        ws = self._agents.get(key)
        if ws is None:
            raise RuntimeError(f"Gateway Agent '{agent_name}' is not connected.")

        query_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending_queries[query_id] = fut

        payload = {
            "action": "query",
            "query_id": query_id,
            "sql": sql,
            "params": params,
            "db_name": db_name,
            "db_type": db_type,
        }

        try:
            await ws.send_json(payload)
            # Wait for response with timeout
            result = await asyncio.wait_for(fut, timeout=timeout)
            if result.get("status") == "error":
                raise RuntimeError(f"Database error on agent: {result.get('error')}")
            return result.get("data", [])
        except asyncio.TimeoutError:
            raise TimeoutError(f"Gateway Agent '{agent_name}' query timed out after {timeout}s.")
        finally:
            self._pending_queries.pop(query_id, None)

    def handle_response(self, response: dict[str, Any]) -> None:
        query_id = response.get("query_id")
        if not query_id:
            return
        fut = self._pending_queries.get(query_id)
        if fut and not fut.done():
            fut.set_result(response)


_gateway_manager: GatewayManager | None = None

def init_gateway_manager() -> GatewayManager:
    global _gateway_manager
    _gateway_manager = GatewayManager()
    return _gateway_manager

def get_gateway_manager() -> GatewayManager:
    if _gateway_manager is None:
        raise RuntimeError("GatewayManager not initialized")
    return _gateway_manager
