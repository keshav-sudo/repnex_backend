"""
TargetPoolRegistry — manages persistent async connection pools per DB connection.

Supported backends
  - PostgreSQL / CloudSQL : asyncpg (pooled, streaming cursor)
  - MSSQL / SysPro        : pymssql via thread-local persistent connections (pooled)
  - MySQL                 : aiomysql (pooled)  [stub — add aiomysql to deps]
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import asyncpg

from app.core.config import get_settings
from app.core.database.models import DBConnection, DBType
from app.core.exceptions import PoolExhausted, TargetDBError
from app.core.logging import get_logger
from app.core.security.encryption import decrypt

log = get_logger(__name__)


# ── MSSQL Thread-Local Connection Pool ────────────────────────────────────────

class MSSQLConnectionPool:
    """
    Thread-local persistent connection pool for pymssql.

    Instead of creating a new TCP connection per query (handshake + TDS login
    every time), we keep ONE persistent connection per thread. Connections
    are validated before use via a lightweight `SELECT 1` health check and
    automatically reconnected if stale or broken.

    This eliminates:
      - TCP handshake latency on every query
      - Ephemeral port exhaustion under concurrent load
      - SQL Server max-connections pressure
      - Timeout errors after laptop sleep / network interruptions
    """

    def __init__(
        self,
        conn_params: dict[str, Any],
        *,
        max_workers: int | None = None,
        max_idle_seconds: int = 300,
        connect_timeout: int = 15,
        query_timeout: int = 60,
        max_retries: int = 2,
    ) -> None:
        self._conn_params = conn_params
        self._max_idle = max_idle_seconds
        self._connect_timeout = connect_timeout
        self._query_timeout = query_timeout
        self._max_retries = max_retries

        # Thread-local storage for persistent connections
        self._local = threading.local()
        # Track all threads that have connections (for cleanup)
        self._connections_lock = threading.Lock()
        self._active_connections: dict[int, Any] = {}  # thread_id -> conn
        self._connection_timestamps: dict[int, float] = {}  # thread_id -> last_used

        workers = max_workers or get_settings().MSSQL_POOL_WORKERS
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="mssql_pool",
        )
        log.info("mssql_pool_created", extra={
            "host": conn_params.get("host"),
            "database": conn_params.get("database"),
            "max_workers": workers,
            "max_idle_s": max_idle_seconds,
        })

    def _get_connection(self):
        """Get or create a persistent connection for the current thread."""
        import pymssql

        tid = threading.current_thread().ident
        conn = getattr(self._local, 'conn', None)
        last_used = getattr(self._local, 'last_used', 0)

        # Check if connection exists and is not too old
        if conn is not None:
            idle_time = time.monotonic() - last_used
            if idle_time > self._max_idle:
                log.debug("mssql_conn_idle_expired", extra={
                    "thread_id": tid, "idle_s": int(idle_time)
                })
                self._close_thread_connection()
                conn = None

        # Validate existing connection with a health check
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                # Connection is alive
                self._local.last_used = time.monotonic()
                return conn
            except Exception as e:
                log.warning("mssql_conn_health_check_failed", extra={
                    "thread_id": tid, "error": str(e)
                })
                self._close_thread_connection()
                conn = None

        # Create new connection
        for attempt in range(1, self._max_retries + 1):
            try:
                conn = pymssql.connect(
                    server=self._conn_params["host"],
                    port=int(self._conn_params["port"]),
                    user=self._conn_params["user"],
                    password=self._conn_params["password"],
                    database=self._conn_params["database"],
                    login_timeout=self._connect_timeout,
                    timeout=self._query_timeout,
                )
                self._local.conn = conn
                self._local.last_used = time.monotonic()

                with self._connections_lock:
                    self._active_connections[tid] = conn
                    self._connection_timestamps[tid] = time.monotonic()

                log.info("mssql_conn_established", extra={
                    "thread_id": tid,
                    "host": self._conn_params["host"],
                    "database": self._conn_params["database"],
                    "attempt": attempt,
                })
                return conn
            except Exception as e:
                log.warning("mssql_conn_attempt_failed", extra={
                    "thread_id": tid, "attempt": attempt,
                    "max_retries": self._max_retries, "error": str(e),
                })
                if attempt == self._max_retries:
                    raise TargetDBError(
                        f"Failed to connect to MSSQL after {self._max_retries} attempts: "
                        f"{e.__class__.__name__}: {e}"
                    ) from e
                # Brief backoff before retry
                time.sleep(min(1.0 * attempt, 3.0))

    def _close_thread_connection(self):
        """Close the connection for the current thread."""
        tid = threading.current_thread().ident
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
            self._local.last_used = 0
            with self._connections_lock:
                self._active_connections.pop(tid, None)
                self._connection_timestamps.pop(tid, None)

    def _execute_with_reconnect(self, sql: str, params: tuple, timeout: int) -> tuple:
        """Execute a query with automatic reconnection on failure."""
        conn = self._get_connection()

        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                # Build column names
                col_names = []
                if cursor.description:
                    for i, desc in enumerate(cursor.description):
                        col_names.append(desc[0] if desc[0] else f"column_{i}")
                return col_names, cursor
        except Exception as e:
            # Connection may be broken — close it and retry once
            log.warning("mssql_query_failed_reconnecting", extra={
                "error": str(e), "sql_len": len(sql),
            })
            self._close_thread_connection()

            # One retry with fresh connection
            conn = self._get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    col_names = []
                    if cursor.description:
                        for i, desc in enumerate(cursor.description):
                            col_names.append(desc[0] if desc[0] else f"column_{i}")
                    return col_names, cursor
            except Exception as retry_err:
                self._close_thread_connection()
                raise TargetDBError(
                    f"MSSQL query failed after reconnection: {retry_err.__class__.__name__}: {retry_err}"
                ) from retry_err

    def execute_streaming(
        self, sql: str, params: tuple, batch_size: int, timeout: int
    ) -> list[list[dict]]:
        """Execute query and return results in batches. Runs in thread pool."""
        conn = self._get_connection()
        max_rows = get_settings().EXECUTOR_MAX_ROWS

        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                col_names = []
                if cursor.description:
                    for i, desc in enumerate(cursor.description):
                        col_names.append(desc[0] if desc[0] else f"column_{i}")

                batches: list[list[dict]] = []
                remaining = max_rows
                while remaining > 0:
                    rows = cursor.fetchmany(min(batch_size, remaining))
                    if not rows:
                        break
                    batches.append([dict(zip(col_names, row)) for row in rows])
                    remaining -= len(rows)

                self._local.last_used = time.monotonic()
                return batches
        except Exception as e:
            # Connection may be broken — close and retry once
            log.warning("mssql_stream_failed_reconnecting", extra={"error": str(e)})
            self._close_thread_connection()

            conn = self._get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    col_names = []
                    if cursor.description:
                        for i, desc in enumerate(cursor.description):
                            col_names.append(desc[0] if desc[0] else f"column_{i}")
                    batches = []
                    remaining = max_rows
                    while remaining > 0:
                        rows = cursor.fetchmany(min(batch_size, remaining))
                        if not rows:
                            break
                        batches.append([dict(zip(col_names, row)) for row in rows])
                        remaining -= len(rows)
                    self._local.last_used = time.monotonic()
                    return batches
            except Exception as retry_err:
                self._close_thread_connection()
                raise TargetDBError(
                    f"MSSQL streaming failed after reconnection: "
                    f"{retry_err.__class__.__name__}: {retry_err}"
                ) from retry_err

    def execute_scalar(self, sql: str, params: tuple, timeout: int) -> Any:
        """Execute query and return single scalar value."""
        conn = self._get_connection()

        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                res = cursor.fetchone()
                self._local.last_used = time.monotonic()
                return res[0] if res else None
        except Exception as e:
            log.warning("mssql_scalar_failed_reconnecting", extra={"error": str(e)})
            self._close_thread_connection()

            conn = self._get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    res = cursor.fetchone()
                    self._local.last_used = time.monotonic()
                    return res[0] if res else None
            except Exception as retry_err:
                self._close_thread_connection()
                raise TargetDBError(
                    f"MSSQL scalar query failed after reconnection: "
                    f"{retry_err.__class__.__name__}: {retry_err}"
                ) from retry_err

    def execute_columns(self, sql: str, params: tuple, timeout: int) -> list[str]:
        """Execute query to get column names without fetching data rows."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                col_names = []
                if cursor.description:
                    for i, desc in enumerate(cursor.description):
                        col_names.append(desc[0] if desc[0] else f"column_{i}")
                return col_names
        except Exception as e:
            log.warning("mssql_columns_failed_reconnecting", extra={"error": str(e)})
            self._close_thread_connection()

            conn = self._get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    col_names = []
                    if cursor.description:
                        for i, desc in enumerate(cursor.description):
                            col_names.append(desc[0] if desc[0] else f"column_{i}")
                    return col_names
            except Exception as retry_err:
                self._close_thread_connection()
                raise TargetDBError(
                    f"MSSQL columns query failed after reconnection: {retry_err.__class__.__name__}: {retry_err}"
                ) from retry_err

    def close_all(self):
        """Close all thread-local connections and shut down the executor."""
        with self._connections_lock:
            for tid, conn in list(self._active_connections.items()):
                try:
                    conn.close()
                except Exception:
                    pass
            self._active_connections.clear()
            self._connection_timestamps.clear()
        self._executor.shutdown(wait=False)
        log.info("mssql_pool_closed", extra={
            "host": self._conn_params.get("host"),
            "database": self._conn_params.get("database"),
        })

    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor

    @property
    def stats(self) -> dict[str, Any]:
        with self._connections_lock:
            return {
                "active_connections": len(self._active_connections),
                "host": self._conn_params.get("host"),
                "database": self._conn_params.get("database"),
            }


# ── MySQL Thread-Local Connection Pool ────────────────────────────────────────

class MySQLConnectionPool:
    """
    Thread-local persistent connection pool for pymysql.
    """

    def __init__(
        self,
        conn_params: dict[str, Any],
        *,
        max_workers: int | None = None,
        max_idle_seconds: int = 300,
        connect_timeout: int = 15,
        query_timeout: int = 60,
        max_retries: int = 2,
    ) -> None:
        self._conn_params = conn_params
        self._max_idle = max_idle_seconds
        self._connect_timeout = connect_timeout
        self._query_timeout = query_timeout
        self._max_retries = max_retries

        self._local = threading.local()
        self._connections_lock = threading.Lock()
        self._active_connections: dict[int, Any] = {}
        self._connection_timestamps: dict[int, float] = {}

        workers = max_workers or 16
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="mysql_pool",
        )

    def _get_connection(self):
        import pymysql

        tid = threading.current_thread().ident
        conn = getattr(self._local, 'conn', None)
        last_used = getattr(self._local, 'last_used', 0)

        if conn is not None:
            idle_time = time.monotonic() - last_used
            if idle_time > self._max_idle:
                self._close_thread_connection()
                conn = None

        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                self._local.last_used = time.monotonic()
                return conn
            except Exception:
                self._close_thread_connection()
                conn = None

        for attempt in range(1, self._max_retries + 1):
            try:
                conn = pymysql.connect(
                    host=self._conn_params["host"],
                    port=int(self._conn_params["port"]),
                    user=self._conn_params["user"],
                    password=self._conn_params["password"],
                    database=self._conn_params["database"],
                    connect_timeout=self._connect_timeout,
                    read_timeout=self._query_timeout,
                    write_timeout=self._query_timeout,
                )
                self._local.conn = conn
                self._local.last_used = time.monotonic()

                with self._connections_lock:
                    self._active_connections[tid] = conn
                    self._connection_timestamps[tid] = time.monotonic()
                return conn
            except Exception as e:
                if attempt == self._max_retries:
                    raise TargetDBError(
                        f"Failed to connect to MySQL after {self._max_retries} attempts: "
                        f"{e.__class__.__name__}: {e}"
                    ) from e
                time.sleep(min(1.0 * attempt, 3.0))

    def _close_thread_connection(self):
        tid = threading.current_thread().ident
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
            self._local.last_used = 0
            with self._connections_lock:
                self._active_connections.pop(tid, None)
                self._connection_timestamps.pop(tid, None)

    def execute_streaming(
        self, sql: str, params: tuple, batch_size: int, timeout: int
    ) -> list[list[dict]]:
        conn = self._get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(sql, params)
                batches = []
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    batches.append(rows)
                self._local.last_used = time.monotonic()
                return batches
        except Exception as e:
            self._close_thread_connection()
            conn = self._get_connection()
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                    cursor.execute(sql, params)
                    batches = []
                    while True:
                        rows = cursor.fetchmany(batch_size)
                        if not rows:
                            break
                        batches.append(rows)
                    self._local.last_used = time.monotonic()
                    return batches
            except Exception as retry_err:
                self._close_thread_connection()
                raise TargetDBError(f"MySQL streaming failed: {retry_err}") from retry_err

    def execute_scalar(self, sql: str, params: tuple, timeout: int) -> Any:
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                res = cursor.fetchone()
                self._local.last_used = time.monotonic()
                return res[0] if res else None
        except Exception as e:
            self._close_thread_connection()
            conn = self._get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    res = cursor.fetchone()
                    self._local.last_used = time.monotonic()
                    return res[0] if res else None
            except Exception as retry_err:
                self._close_thread_connection()
                raise TargetDBError(f"MySQL scalar query failed: {retry_err}") from retry_err

    def execute_columns(self, sql: str, params: tuple, timeout: int) -> list[str]:
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                col_names = []
                if cursor.description:
                    for i, desc in enumerate(cursor.description):
                        col_names.append(desc[0] if desc[0] else f"column_{i}")
                return col_names
        except Exception as e:
            self._close_thread_connection()
            conn = self._get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    col_names = []
                    if cursor.description:
                        for i, desc in enumerate(cursor.description):
                            col_names.append(desc[0] if desc[0] else f"column_{i}")
                    return col_names
            except Exception as retry_err:
                self._close_thread_connection()
                raise TargetDBError(f"MySQL columns query failed: {retry_err}") from retry_err

    def close_all(self):
        with self._connections_lock:
            for tid, conn in list(self._active_connections.items()):
                try:
                    conn.close()
                except Exception:
                    pass
            self._active_connections.clear()
            self._connection_timestamps.clear()
        self._executor.shutdown(wait=False)

    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_positional_pg(sql: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
    """Convert %(name)s style → $1..$N for asyncpg."""
    ordered_keys: list[str] = []

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key not in ordered_keys:
            ordered_keys.append(key)
        return f"${ordered_keys.index(key) + 1}"

    pg_sql = re.sub(r"%\((\w+)\)s", _replace, sql)
    bound = [params[k] for k in ordered_keys]
    return pg_sql, bound


def _to_positional_mssql(sql: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
    """Convert %(name)s → %s placeholders for pymssql."""
    ordered_keys: list[str] = []

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        ordered_keys.append(key)
        return "%s"

    mssql_sql = re.sub(r"%\((\w+)\)s", _replace, sql)
    bound = [params[k] for k in ordered_keys]
    return mssql_sql, bound


# ── TargetPool ────────────────────────────────────────────────────────────────

class TargetPool:
    """Thin adapter over driver-specific async pools."""

    def __init__(
        self,
        db_type: DBType,
        raw_pool: Any,
        *,
        conn_params: dict[str, Any] | None = None,
        mssql_pool: MSSQLConnectionPool | None = None,
        mysql_pool: MySQLConnectionPool | None = None,
    ) -> None:
        self.db_type = db_type
        self._pool = raw_pool             # asyncpg.Pool for PG, None for MSSQL
        self._conn_params = conn_params   # MSSQL only (host, port, user, password, db)
        self._mssql_pool = mssql_pool     # Thread-local connection pool for MSSQL
        self._mysql_pool = mysql_pool     # Thread-local connection pool for MySQL

    # ── Public streaming interface ─────────────────────────────────────────

    async def fetch_stream(
        self, sql: str, params: dict[str, Any], *, batch_size: int, timeout: float
    ):
        if self._conn_params and self._conn_params.get("gateway"):
            async for batch in self._fetch_gateway(sql, params, batch_size, timeout):
                yield batch
            return

        if self.db_type in (DBType.postgres, DBType.cloudsql):
            async for batch in self._fetch_pg(sql, params, batch_size, timeout):
                yield batch
        elif self.db_type == DBType.mssql:
            async for batch in self._fetch_mssql(sql, params, batch_size, timeout):
                yield batch
        elif self.db_type == DBType.mysql:
            async for batch in self._fetch_mysql(sql, params, batch_size, timeout):
                yield batch
        else:
            raise TargetDBError(f"Streaming not yet implemented for {self.db_type.value}")

    # ── PostgreSQL ─────────────────────────────────────────────────────────

    async def _fetch_pg(
        self, sql: str, params: dict[str, Any], batch_size: int, timeout: float
    ):
        pg_sql, bound = _to_positional_pg(sql, params)
        log.debug("target_pg_query", extra={"sql_len": len(pg_sql), "n_params": len(bound)})

        try:
            async with asyncio.timeout(timeout):
                async with self._pool.acquire() as conn:
                    # Use server-side cursor via connection.cursor() for streaming
                    stmt = await conn.prepare(pg_sql)
                    async with conn.transaction():
                        batch: list[dict[str, Any]] = []
                        async for record in stmt.cursor(*bound, prefetch=batch_size):
                            batch.append(dict(record))
                            if len(batch) >= batch_size:
                                yield batch
                                batch = []
                        if batch:
                            yield batch
        except asyncpg.PostgresError as e:
            raise TargetDBError(f"Target DB error: {e.__class__.__name__}: {e}") from e
        except asyncio.TimeoutError as e:
            raise TargetDBError("Target DB query timed out") from e

    # ── MSSQL / SysPro via pymssql (persistent thread-local connections) ──

    async def _fetch_mssql(
        self, sql: str, params: dict[str, Any], batch_size: int, timeout: float
    ):
        mssql_sql, bound = _to_positional_mssql(sql, params)
        log.debug("target_mssql_query", extra={"sql_len": len(mssql_sql), "n_params": len(bound)})

        loop = asyncio.get_running_loop()

        try:
            batches = await asyncio.wait_for(
                loop.run_in_executor(
                    self._mssql_pool.executor,
                    self._mssql_pool.execute_streaming,
                    mssql_sql, tuple(bound), batch_size, int(timeout),
                ),
                timeout=timeout + 5,  # Extra grace for connection establishment
            )
        except asyncio.TimeoutError as e:
            raise TargetDBError("MSSQL query timed out") from e
        except TargetDBError:
            raise
        except Exception as e:
            raise TargetDBError(f"MSSQL error: {e.__class__.__name__}: {e}") from e

        for batch in batches:
            yield batch

    async def _fetch_mysql(
        self, sql: str, params: dict[str, Any], batch_size: int, timeout: float
    ):
        mysql_sql, bound = _to_positional_mssql(sql, params)
        log.debug("target_mysql_query", extra={"sql_len": len(mysql_sql), "n_params": len(bound)})

        loop = asyncio.get_running_loop()

        try:
            batches = await asyncio.wait_for(
                loop.run_in_executor(
                    self._mysql_pool.executor,
                    self._mysql_pool.execute_streaming,
                    mysql_sql, tuple(bound), batch_size, int(timeout),
                ),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError as e:
            raise TargetDBError("MySQL query timed out") from e
        except TargetDBError:
            raise
        except Exception as e:
            raise TargetDBError(f"MySQL error: {e.__class__.__name__}: {e}") from e

        for batch in batches:
            yield batch

    async def get_columns(self, sql: str, params: dict[str, Any], timeout: float) -> list[str]:
        if self._conn_params and self._conn_params.get("gateway"):
            return []

        if self.db_type in (DBType.postgres, DBType.cloudsql):
            pg_sql, bound = _to_positional_pg(sql, params)
            try:
                async with asyncio.timeout(timeout):
                    async with self._pool.acquire() as conn:
                        stmt = await conn.prepare(pg_sql)
                        return [attr.name for attr in stmt.get_attributes()]
            except Exception as e:
                log.warning("failed_to_get_pg_columns", extra={"error": str(e)})
                return []
        elif self.db_type == DBType.mssql:
            mssql_sql, bound = _to_positional_mssql(sql, params)
            loop = asyncio.get_running_loop()
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        self._mssql_pool.executor,
                        self._mssql_pool.execute_columns,
                        mssql_sql, tuple(bound), int(timeout),
                    ),
                    timeout=timeout + 5,
                )
            except Exception as e:
                log.warning("failed_to_get_mssql_columns", extra={"error": str(e)})
                return []
        elif self.db_type == DBType.mysql:
            mysql_sql, bound = _to_positional_mssql(sql, params)
            loop = asyncio.get_running_loop()
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        self._mysql_pool.executor,
                        self._mysql_pool.execute_columns,
                        mysql_sql, tuple(bound), int(timeout),
                    ),
                    timeout=timeout + 5,
                )
            except Exception as e:
                log.warning("failed_to_get_mysql_columns", extra={"error": str(e)})
                return []
        return []

    # ── Scalar helper ──────────────────────────────────────────────────────

    async def execute_one(self, sql: str, params: dict[str, Any], *, timeout: float) -> Any:
        if self._conn_params and self._conn_params.get("gateway"):
            return await self._execute_one_gateway(sql, params, timeout=timeout)

        if self.db_type in (DBType.postgres, DBType.cloudsql):
            pg_sql, bound = _to_positional_pg(sql, params)
            async with asyncio.timeout(timeout):
                async with self._pool.acquire() as conn:
                    return await conn.fetchval(pg_sql, *bound)
        elif self.db_type == DBType.mssql:
            mssql_sql, bound = _to_positional_mssql(sql, params)
            loop = asyncio.get_running_loop()

            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        self._mssql_pool.executor,
                        self._mssql_pool.execute_scalar,
                        mssql_sql, tuple(bound), int(timeout),
                    ),
                    timeout=timeout + 5,
                )
            except asyncio.TimeoutError as e:
                raise TargetDBError("MSSQL scalar query timed out") from e
            except TargetDBError:
                raise
            except Exception as e:
                raise TargetDBError(f"MSSQL scalar error: {e.__class__.__name__}: {e}") from e
        elif self.db_type == DBType.mysql:
            mysql_sql, bound = _to_positional_mssql(sql, params)
            loop = asyncio.get_running_loop()

            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        self._mysql_pool.executor,
                        self._mysql_pool.execute_scalar,
                        mysql_sql, tuple(bound), int(timeout),
                    ),
                    timeout=timeout + 5,
                )
            except asyncio.TimeoutError as e:
                raise TargetDBError("MySQL scalar query timed out") from e
            except TargetDBError:
                raise
            except Exception as e:
                raise TargetDBError(f"MySQL scalar error: {e.__class__.__name__}: {e}") from e
        raise TargetDBError(f"execute_one not implemented for {self.db_type.value}")

    async def _fetch_gateway(
        self, sql: str, params: dict[str, Any], batch_size: int, timeout: float
    ):
        from app.services.gateway_manager import get_gateway_manager
        mgr = get_gateway_manager()
        org_id = self._conn_params["org_id"]
        agent_name = self._conn_params["agent_name"]
        db_name = self._conn_params["database"]
        db_type = self._conn_params["db_type"]
        
        rows = await mgr.execute_query(
            org_id=org_id,
            agent_name=agent_name,
            sql=sql,
            params=params,
            db_name=db_name,
            db_type=db_type,
            timeout=timeout,
        )
        for i in range(0, len(rows), batch_size):
            yield rows[i : i + batch_size]

    async def _execute_one_gateway(self, sql: str, params: dict[str, Any], *, timeout: float) -> Any:
        from app.services.gateway_manager import get_gateway_manager
        mgr = get_gateway_manager()
        org_id = self._conn_params["org_id"]
        agent_name = self._conn_params["agent_name"]
        db_name = self._conn_params["database"]
        db_type = self._conn_params["db_type"]
        
        rows = await mgr.execute_query(
            org_id=org_id,
            agent_name=agent_name,
            sql=sql,
            params=params,
            db_name=db_name,
            db_type=db_type,
            timeout=timeout,
        )
        if rows and len(rows) > 0:
            first = rows[0]
            if isinstance(first, dict):
                return list(first.values())[0]
            elif isinstance(first, (list, tuple)):
                return first[0]
            return first
        return None

    async def close(self) -> None:
        try:
            if self._pool is not None:
                await self._pool.close()
            if self._mssql_pool is not None:
                self._mssql_pool.close_all()
            if self._mysql_pool is not None:
                self._mysql_pool.close_all()
        except Exception:
            log.exception("error_closing_target_pool")


# ── TargetPoolRegistry ────────────────────────────────────────────────────────

class TargetPoolRegistry:
    """
    LRU cache of TargetPool instances keyed by connection-UUID.

    A pool is created on first use and kept alive for subsequent queries —
    this avoids reconnect latency on every request.
    """

    def __init__(self) -> None:
        s = get_settings()
        self._cache: OrderedDict[uuid.UUID, TargetPool] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max = s.TARGET_POOL_MAX           # LRU eviction threshold
        self._min_size = s.TARGET_POOL_MIN_SIZE  # idle connections per pool
        self._max_size = s.TARGET_POOL_MAX_SIZE  # peak connections per pool

    async def get_pool(self, conn: DBConnection) -> TargetPool:
        async with self._lock:
            if conn.id in self._cache:
                self._cache.move_to_end(conn.id)
                return self._cache[conn.id]

            log.info("target_pool_create", extra={"conn_id": str(conn.id), "db_type": conn.db_type.value})
            pool = await self._build(conn)
            self._cache[conn.id] = pool
            self._cache.move_to_end(conn.id)

            # Evict least-recently-used pools once threshold is exceeded
            while len(self._cache) > self._max:
                evict_id, evicted = self._cache.popitem(last=False)
                log.info("target_pool_evict", extra={"conn_id": str(evict_id)})
                await evicted.close()

            return pool

    async def evict(self, conn_id: uuid.UUID) -> None:
        async with self._lock:
            pool = self._cache.pop(conn_id, None)
        if pool is not None:
            log.info("target_pool_manual_evict", extra={"conn_id": str(conn_id)})
            await pool.close()

    async def _build(self, conn: DBConnection) -> TargetPool:
        # ── Gateway Mode ───────────────────────────────────────────────────
        if conn.host.startswith("gateway:") or conn.host == "gateway":
            agent_name = conn.host.split("gateway:")[1] if "gateway:" in conn.host else "default"
            conn_params = {
                "gateway": True,
                "org_id": conn.org_id,
                "agent_name": agent_name,
                "database": conn.db_name,
                "db_type": conn.db_type.value,
            }
            return TargetPool(conn.db_type, None, conn_params=conn_params)

        username = decrypt(conn.encrypted_username)
        password = decrypt(conn.encrypted_password)

        # ── PostgreSQL / CloudSQL ──────────────────────────────────────────
        if conn.db_type in (DBType.postgres, DBType.cloudsql):
            try:
                pool = await asyncpg.create_pool(
                    user=username,
                    password=password,
                    host=conn.host,
                    port=conn.port,
                    database=conn.db_name,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    # Wait up to 10 s for a connection slot
                    timeout=10,
                    # Kill queries that run longer than 30 s at the driver level
                    command_timeout=30,
                    ssl="require" if conn.ssl_enabled else None,
                )
            except (asyncpg.PostgresError, OSError) as e:
                raise PoolExhausted(f"Failed to create PG pool: {e.__class__.__name__}: {e}") from e
            return TargetPool(conn.db_type, pool)

        # ── MSSQL / SysPro (thread-local persistent connection pool) ──────
        if conn.db_type == DBType.mssql:
            conn_params = {
                "host": conn.host,
                "port": conn.port,
                "user": username,
                "password": password,
                "database": conn.db_name,
            }
            mssql_pool = MSSQLConnectionPool(
                conn_params,
                max_idle_seconds=300,      # Close idle connections after 5 min
                connect_timeout=15,        # TCP + TDS handshake timeout
                query_timeout=60,          # Per-query timeout
                max_retries=2,             # Retry connection attempts
            )
            return TargetPool(conn.db_type, None, conn_params=conn_params, mssql_pool=mssql_pool)

        # ── MySQL (thread-local persistent connection pool) ───────────────────
        if conn.db_type == DBType.mysql:
            conn_params = {
                "host": conn.host,
                "port": conn.port,
                "user": username,
                "password": password,
                "database": conn.db_name,
            }
            mysql_pool = MySQLConnectionPool(
                conn_params,
                max_idle_seconds=300,
                connect_timeout=15,
                query_timeout=60,
                max_retries=2,
            )
            return TargetPool(conn.db_type, None, conn_params=conn_params, mysql_pool=mysql_pool)

        raise TargetDBError(f"Unsupported db_type: {conn.db_type.value}")

    async def close(self) -> None:
        async with self._lock:
            for pool in self._cache.values():
                await pool.close()
            self._cache.clear()


# ── Module-level singleton ────────────────────────────────────────────────────

_registry: TargetPoolRegistry | None = None


def init_target_pool_registry() -> TargetPoolRegistry:
    global _registry
    _registry = TargetPoolRegistry()
    return _registry


def get_target_pool_registry() -> TargetPoolRegistry:
    if _registry is None:
        raise RuntimeError("TargetPoolRegistry not initialized")
    return _registry


async def close_target_pool_registry() -> None:
    global _registry
    if _registry is not None:
        await _registry.close()
        _registry = None
