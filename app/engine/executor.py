from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.database.models import DBConnection
from app.core.database.target_pool import get_target_pool_registry
from app.core.exceptions import TargetDBError
from app.engine.parameter_binder import BoundQuery


@dataclass(slots=True)
class ExecutionResult:
    rows: list[dict[str, Any]]
    rows_returned: int
    execution_time_ms: int
    truncated: bool
    columns: list[str] | None = None


async def execute_collect(conn: DBConnection, bound: BoundQuery) -> ExecutionResult:
    s = get_settings()
    rows: list[dict[str, Any]] = []
    truncated = False
    started = time.perf_counter()
    async for batch in execute_stream(conn, bound):
        rows.extend(batch)
        if len(rows) > s.EXECUTOR_MAX_ROWS:
            truncated = True
            rows = rows[: s.EXECUTOR_MAX_ROWS]
            break

    columns = []
    if rows:
        columns = list(rows[0].keys())
    else:
        try:
            pool = await get_target_pool_registry().get_pool(conn)
            columns = await pool.get_columns(
                bound.sql,
                bound.params,
                timeout=s.EXECUTOR_TIMEOUT_S,
            )
        except Exception:
            from app.core.logging import get_logger
            log = get_logger(__name__)
            log.warning("failed_to_retrieve_empty_columns", exc_info=True)
            columns = []

    return ExecutionResult(
        rows=rows,
        rows_returned=len(rows),
        execution_time_ms=int((time.perf_counter() - started) * 1000),
        truncated=truncated,
        columns=columns,
    )


async def execute_stream(
    conn: DBConnection, bound: BoundQuery
) -> AsyncIterator[list[dict[str, Any]]]:
    s = get_settings()
    pool = await get_target_pool_registry().get_pool(conn)
    sent = 0
    try:
        async for batch in pool.fetch_stream(
            bound.sql,
            bound.params,
            batch_size=s.EXECUTOR_BATCH_SIZE,
            timeout=s.EXECUTOR_TIMEOUT_S,
        ):
            if sent + len(batch) > s.EXECUTOR_MAX_ROWS:
                allowed = s.EXECUTOR_MAX_ROWS - sent
                if allowed > 0:
                    yield batch[:allowed]
                return
            sent += len(batch)
            yield batch
    except TargetDBError:
        raise
    except Exception as e:  # pragma: no cover
        raise TargetDBError(f"Unexpected target error: {str(e)}") from e

