"""MySQL thread-local persistent connection pool."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pymysql
from app.core.exceptions import TargetDBError
from app.core.logging import get_logger

log = get_logger(__name__)

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
        except Exception:
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
        except Exception:
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
        except Exception:
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

