from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.database.models import DBConnection, DBType
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

    # ── MongoDB: native executor, bypass SQL pool ─────────────────────────
    if conn.db_type == DBType.mongodb:
        from app.engine.mongo_executor import execute_mongo_collect
        try:
            rows, columns, elapsed_ms = await execute_mongo_collect(
                conn, bound.sql, max_rows=s.EXECUTOR_MAX_ROWS
            )
        except TargetDBError:
            raise
        except Exception as exc:
            raise TargetDBError(f"MongoDB execution error: {exc}") from exc

        truncated = len(rows) >= s.EXECUTOR_MAX_ROWS
        return ExecutionResult(
            rows=rows,
            rows_returned=len(rows),
            execution_time_ms=elapsed_ms,
            truncated=truncated,
            columns=columns,
        )

    # ── SQL databases: existing pool registry path ────────────────────────
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


def clean_row_data(val: Any) -> Any:
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return f"0x{val.hex()}"
    elif isinstance(val, dict):
        return {k: clean_row_data(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [clean_row_data(x) for x in val]
    return val


async def execute_stream(
    conn: DBConnection, bound: BoundQuery
) -> AsyncIterator[list[dict[str, Any]]]:
    """Streaming executor — SQL databases only. MongoDB uses execute_collect directly."""
    s = get_settings()

    if conn.db_type == DBType.mongodb:
        # MongoDB doesn't stream via the pool — delegate to collect
        from app.engine.mongo_executor import execute_mongo_collect
        rows, _cols, _ms = await execute_mongo_collect(conn, bound.sql, max_rows=s.EXECUTOR_MAX_ROWS)
        yield rows
        return

    pool = await get_target_pool_registry().get_pool(conn)
    sent = 0
    try:
        async for batch in pool.fetch_stream(
            bound.sql,
            bound.params,
            batch_size=s.EXECUTOR_BATCH_SIZE,
            timeout=s.EXECUTOR_TIMEOUT_S,
        ):
            cleaned_batch = [
                {k: clean_row_data(v) for k, v in row.items()}
                for row in batch
            ]
            if sent + len(cleaned_batch) > s.EXECUTOR_MAX_ROWS:
                allowed = s.EXECUTOR_MAX_ROWS - sent
                if allowed > 0:
                    yield cleaned_batch[:allowed]
                return
            sent += len(cleaned_batch)
            yield cleaned_batch
    except TargetDBError:
        raise
    except Exception as e:  # pragma: no cover
        raise TargetDBError(f"Unexpected target error: {str(e)}") from e


