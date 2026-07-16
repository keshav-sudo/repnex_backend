from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.database.models import DBConnection, DBType
from app.engine.executor import execute_collect
from app.engine.parameter_binder import BoundQuery


@pytest.mark.asyncio
async def test_execute_collect_empty_rows_resolves_columns(settings):
    # Mock pool and registry
    mock_pool = AsyncMock()
    mock_pool.get_columns = AsyncMock(return_value=["col1", "col2"])

    mock_registry = MagicMock()
    mock_registry.get_pool = AsyncMock(return_value=mock_pool)

    conn = MagicMock(spec=DBConnection)
    conn.db_type = DBType.mssql   # ensure it takes the SQL path, not MongoDB
    bound = MagicMock(spec=BoundQuery)
    bound.sql = "SELECT * FROM test"
    bound.params = {}

    # Mock execute_stream to yield no batch (empty rows result)
    async def mock_execute_stream(*args, **kwargs):
        if False:
            yield []

    with patch("app.engine.executor.get_target_pool_registry", return_value=mock_registry), \
         patch("app.engine.executor.execute_stream", mock_execute_stream):

        result = await execute_collect(conn, bound)
        assert result.rows == []
        assert result.rows_returned == 0
        assert result.columns == ["col1", "col2"]
