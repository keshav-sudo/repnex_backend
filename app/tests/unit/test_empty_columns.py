from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.database.models import DBConnection
from app.query_engine.executor import execute_collect
from app.query_engine.parameter_binder import BoundQuery


@pytest.mark.asyncio
async def test_execute_collect_empty_rows_resolves_columns(settings):
    # Mock pool and registry
    mock_pool = AsyncMock()
    mock_pool.get_columns = AsyncMock(return_value=["col1", "col2"])

    mock_registry = MagicMock()
    mock_registry.get_pool = AsyncMock(return_value=mock_pool)

    conn = MagicMock(spec=DBConnection)
    bound = MagicMock(spec=BoundQuery)
    bound.sql = "SELECT * FROM test"
    bound.params = {}

    # Mock execute_stream to yield no batch (empty rows result)
    async def mock_execute_stream(*args, **kwargs):
        if False:
            yield []

    with patch("app.query_engine.executor.get_target_pool_registry", return_value=mock_registry), \
         patch("app.query_engine.executor.execute_stream", mock_execute_stream):

        result = await execute_collect(conn, bound)
        assert result.rows == []
        assert result.rows_returned == 0
        assert result.columns == ["col1", "col2"]
