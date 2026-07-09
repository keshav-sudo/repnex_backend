from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.core.logging import get_logger
from fastapi import WebSocket

log = get_logger(__name__)

class DatabaseQueryError(Exception):
    """Exception raised when a query fails on the gateway agent database side."""
    pass

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

    async def unregister(self, org_id: uuid.UUID | str, agent_name: str, websocket: WebSocket) -> None:
        key = f"{org_id}:{agent_name}"
        async with self._lock:
            if self._agents.get(key) == websocket:
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

        # Try sending the query, with up to 1 retry if connection is lost or stale
        for attempt in range(2):
            ws = self._agents.get(key)

            # Check if WebSocket is missing or not connected
            is_valid = True
            if ws is None:
                is_valid = False
            else:
                try:
                    if ws.client_state.name != "CONNECTED":
                        is_valid = False
                except AttributeError:
                    pass

            # If not valid, wait up to 15 seconds for the agent to reconnect and register
            if not is_valid:
                log.info("gateway_agent_offline_waiting", extra={"agent_name": agent_name, "attempt": attempt})
                wait_start = asyncio.get_event_loop().time()
                while asyncio.get_event_loop().time() - wait_start < 15.0:
                    ws = self._agents.get(key)
                    if ws is not None:
                        try:
                            if ws.client_state.name == "CONNECTED":
                                is_valid = True
                                break
                        except AttributeError:
                            is_valid = True
                            break
                    await asyncio.sleep(0.5)

                if not is_valid or ws is None:
                    active = self.list_active_agents(org_id)
                    if active:
                        raise RuntimeError(
                            f"Gateway Agent '{agent_name}' is not connected. "
                            f"(Currently connected agents for your organization: {active}). "
                            f"Please check if the agent script is running with the correct --agent-name parameter."
                        )
                    raise RuntimeError(
                        f"Gateway Agent '{agent_name}' is not connected. "
                        f"No active agents are currently connected for your organization. "
                        f"Please verify if the agent script is running on the host machine."
                    )

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
            from fastapi.encoders import jsonable_encoder
            serialized_payload = jsonable_encoder(payload)

            try:
                try:
                    await ws.send_json(serialized_payload)
                    # Wait for response with timeout
                    result = await asyncio.wait_for(fut, timeout=timeout)
                    if result.get("status") == "error":
                        raise DatabaseQueryError(result.get('error'))
                    return result.get("data", [])
                except DatabaseQueryError as e:
                    raise RuntimeError(f"Database error on agent: {str(e)}") from e
                except TimeoutError:
                    raise TimeoutError(f"Gateway Agent '{agent_name}' query timed out after {timeout}s.")
                except Exception as e:
                    # Connection was lost or closed while sending/waiting
                    async with self._lock:
                        if self._agents.get(key) == ws:
                            self._agents.pop(key, None)

                    if attempt == 0:
                        log.warning("gateway_query_attempt_failed_retrying", extra={"agent_name": agent_name, "error": str(e)})
                        await asyncio.sleep(0.5)
                        continue
                    raise RuntimeError(f"Failed to communicate with Gateway Agent '{agent_name}': {str(e)}") from e
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
