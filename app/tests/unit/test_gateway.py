from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import WebSocket

from app.services.gateway_manager import GatewayManager

@pytest.mark.asyncio
async def test_gateway_manager_flow():
    mgr = GatewayManager()
    org_id = uuid.uuid4()
    agent_name = "test-agent"

    # Initially not connected
    assert not mgr.is_agent_active(org_id, agent_name)
    assert mgr.list_active_agents(org_id) == []

    # Mock WebSocket
    mock_ws = AsyncMock(spec=WebSocket)
    
    # Register
    await mgr.register(org_id, agent_name, mock_ws)
    assert mgr.is_agent_active(org_id, agent_name)
    assert mgr.list_active_agents(org_id) == ["test-agent"]

    # Start executing query in background task since it waits for websocket response
    query_task = asyncio.create_task(
        mgr.execute_query(
            org_id=org_id,
            agent_name=agent_name,
            sql="SELECT * FROM users WHERE id = :id",
            params={"id": 10},
            db_name="test_db",
            db_type="postgres",
            timeout=2.0
        )
    )

    # Yield to let query_task run and send message
    await asyncio.sleep(0.01)

    # Check mock websocket received the correct payload
    mock_ws.send_json.assert_called_once()
    sent_payload = mock_ws.send_json.call_args[0][0]
    assert sent_payload["action"] == "query"
    assert sent_payload["sql"] == "SELECT * FROM users WHERE id = :id"
    assert sent_payload["params"] == {"id": 10}
    assert sent_payload["db_name"] == "test_db"
    assert sent_payload["db_type"] == "postgres"
    
    query_id = sent_payload["query_id"]

    # Simulate response from agent
    response_payload = {
        "action": "query_response",
        "query_id": query_id,
        "status": "success",
        "data": [{"id": 10, "username": "alice"}]
    }
    
    mgr.handle_response(response_payload)

    # Wait for the execute_query task to finish and get result
    result = await query_task
    assert result == [{"id": 10, "username": "alice"}]

    # Unregister
    await mgr.unregister(org_id, agent_name)
    assert not mgr.is_agent_active(org_id, agent_name)
