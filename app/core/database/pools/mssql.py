"""MSSQL thread-local persistent connection pool."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.core.config import get_settings
from app.core.exceptions import TargetDBError
from app.core.logging import get_logger

log = get_logger(__name__)

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
                    batches.append([dict(zip(col_names, row, strict=False)) for row in rows])
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
                        batches.append([dict(zip(col_names, row, strict=False)) for row in rows])
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

