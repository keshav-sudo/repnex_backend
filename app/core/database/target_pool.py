"""
TargetPoolRegistry — manages persistent async connection pools per DB connection.

Supported backends
  - PostgreSQL / CloudSQL : asyncpg (pooled, streaming cursor)
  - MSSQL / SysPro        : pyodbc via run_in_executor (sync driver, thread-pooled)
  - MySQL                 : aiomysql (pooled)  [stub — add aiomysql to deps]
"""
from __future__ import annotations

import asyncio
import re
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

# Thread-pool for synchronous MSSQL calls via pymssql
_MSSQL_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="mssql_pymssql")


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

    def __init__(self, db_type: DBType, raw_pool: Any, *, conn_params: dict[str, Any] | None = None) -> None:
        self.db_type = db_type
        self._pool = raw_pool             # asyncpg.Pool for PG, None for MSSQL
        self._conn_params = conn_params   # MSSQL only (host, port, user, password, db)

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
                        async for record in stmt.cursor(*bound, prefetch=batch_size):
                            yield [dict(record)]
        except asyncpg.PostgresError as e:
            raise TargetDBError(f"Target DB error: {e.__class__.__name__}: {e}") from e
        except asyncio.TimeoutError as e:
            raise TargetDBError("Target DB query timed out") from e

    # ── MSSQL / SysPro via pymssql (sync → thread-pool) ───────────────────

    async def _fetch_mssql(
        self, sql: str, params: dict[str, Any], batch_size: int, timeout: float
    ):
        mssql_sql, bound = _to_positional_mssql(sql, params)
        log.debug("target_mssql_query", extra={"sql_len": len(mssql_sql), "n_params": len(bound)})

        loop = asyncio.get_running_loop()

        def _run_sync() -> list[list[dict]]:
            import pymssql  # pure python driver
            # Connect to SQL Server
            with pymssql.connect(
                server=self._conn_params["host"],
                port=int(self._conn_params["port"]),
                user=self._conn_params["user"],
                password=self._conn_params["password"],
                database=self._conn_params["database"],
                timeout=int(timeout),
                login_timeout=10,
            ) as raw_conn:
                with raw_conn.cursor() as cursor:
                    cursor.execute(mssql_sql, tuple(bound))
                    # Build column names with fallback for unnamed cols
                    col_names = []
                    if cursor.description:
                        for i, desc in enumerate(cursor.description):
                            col_names.append(desc[0] if desc[0] else f"column_{i}")
                    batches: list[list[dict]] = []
                    while True:
                        rows = cursor.fetchmany(batch_size)
                        if not rows:
                            break
                        batches.append([dict(zip(col_names, row)) for row in rows])
                    return batches

        try:
            batches = await asyncio.wait_for(
                loop.run_in_executor(_MSSQL_EXECUTOR, _run_sync),
                timeout=timeout + 2,
            )
        except asyncio.TimeoutError as e:
            raise TargetDBError("MSSQL query timed out") from e
        except Exception as e:
            raise TargetDBError(f"MSSQL error: {e.__class__.__name__}: {e}") from e

        for batch in batches:
            yield batch

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

            def _run_sync() -> Any:
                import pymssql
                with pymssql.connect(
                    server=self._conn_params["host"],
                    port=int(self._conn_params["port"]),
                    user=self._conn_params["user"],
                    password=self._conn_params["password"],
                    database=self._conn_params["database"],
                    timeout=int(timeout),
                    login_timeout=10,
                ) as raw_conn:
                    with raw_conn.cursor() as cursor:
                        cursor.execute(mssql_sql, tuple(bound))
                        res = cursor.fetchone()
                        return res[0] if res else None

            return await loop.run_in_executor(_MSSQL_EXECUTOR, _run_sync)
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

        # ── MSSQL / SysPro ────────────────────────────────────────────────
        if conn.db_type == DBType.mssql:
            conn_params = {
                "host": conn.host,
                "port": conn.port,
                "user": username,
                "password": password,
                "database": conn.db_name,
            }
            return TargetPool(conn.db_type, None, conn_params=conn_params)

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
