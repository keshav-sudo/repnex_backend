from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.core.security.auth import CurrentUser
from app.services.chat.diagnostic_service import detect_and_run_diagnostic


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def current_user():
    return CurrentUser(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email="test@example.com",
        role="admin",
        module_permissions={},
    )


@pytest.mark.asyncio
@patch("app.services.chat.diagnostic_service.get_llm")
@patch("app.services.chat.diagnostic_service.connection_service")
@patch("app.services.chat.diagnostic_service.execute_collect")
async def test_diagnostic_service_success(
    mock_execute_collect,
    mock_connection_service,
    mock_get_llm,
    mock_db,
    current_user,
):
    # Mock classifier return
    mock_llm = MagicMock()
    mock_llm.chat_json = AsyncMock(return_value={
        "is_diagnostic": True,
        "customer_name": "Customer 1",
        "period_description": "April"
    })
    mock_llm.chat_text = AsyncMock(return_value="AI summary explanation")
    mock_get_llm.return_value = mock_llm

    # Mock connection service
    mock_conn = MagicMock()
    mock_conn.db_type.value = "postgres"
    mock_connection_service.get_connection = AsyncMock(return_value=mock_conn)

    # Mock database results:
    # 1. Resolve customer code query
    mock_resolve_res = MagicMock()
    mock_resolve_res.rows = [{"Customer": "CUST001"}]
    
    # 2. Fetch history query
    mock_history_res = MagicMock()
    mock_history_res.rows = [
        # Peak Period (March)
        {"OrderDate": "2026-03-10", "ProductCode": "PRODA", "MShipQty": 10.0, "MPrice": 100.0, "MUnitCost": 60.0},
        # Drop Period (April)
        {"OrderDate": "2026-04-15", "ProductCode": "PRODA", "MShipQty": 10.0, "MPrice": 100.0, "MUnitCost": 80.0}, # unit cost increased to 80
    ]
    mock_history_res.execution_time_ms = 45

    mock_execute_collect.side_effect = [mock_resolve_res, mock_history_res]

    response = await detect_and_run_diagnostic(
        db=mock_db,
        current=current_user,
        connection_id=uuid.uuid4(),
        natural_language="Why did Customer 1's margin drop in April?"
    )

    assert response is not None
    assert response.type == "executable"
    assert response.template_id == "diagnostic_variance_analysis"
    assert "CUST001" in response.template_description
    assert response.rows is not None
    
    # We should have aggregated results in the response rows
    categories = [r["Category"] for r in response.rows]
    assert "Cost Impact (Supplier pricing)" in categories
    assert "Price Impact (Discounts)" in categories
    
    # Verify cost impact was negative since unit cost went up
    cost_impact_row = next(r for r in response.rows if r["Category"] == "Cost Impact (Supplier pricing)")
    assert cost_impact_row["Impact ($)"] < 0
