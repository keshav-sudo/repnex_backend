from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from typing import Any

import asyncpg

from app.core.config import get_settings
from app.core.database.models import DBConnection, DBType
from app.core.exceptions import PoolExhausted, TargetDBError
from app.core.logging import get_logger
from app.core.security.encryption import decrypt

log = get_logger(__name__)


class TargetPool:
    """Adapter over driver-specific async pools. Currently only postgres has
    a streaming impl; mysql/mssql/oracle adapters can be added behind the same
    interface."""

    def __init__(self, db_type: DBType, raw_pool: Any) -> None:
        self.db_type = db_type
        self._pool = raw_pool

    async def fetch_stream(
        self, sql: str, params: dict[str, Any], *, batch_size: int, timeout: float
    ):
        if self.db_type in (DBType.postgres, DBType.cloudsql):
            async for batch in self._fetch_pg(sql, params, batch_size, timeout):
                yield batch
        else:
            raise TargetDBError(f"Streaming not yet implemented for {self.db_type.value}")

    async def _fetch_pg(self, sql: str, params: dict[str, Any], batch_size: int, timeout: float):
        # Convert %(name)s placeholders to $1..$N for asyncpg.
        ordered_keys: list[str] = []

        def _replace(match):
            key = match.group(1)
            if key not in ordered_keys:
                ordered_keys.append(key)
            return f"${ordered_keys.index(key) + 1}"

        import re

        pg_sql = re.sub(r"%\((\w+)\)s", _replace, sql)
        bound = [params[k] for k in ordered_keys]

        async with self._pool.acquire() as conn:
            try:
                async with conn.transaction():
                    cursor = await conn.cursor(pg_sql, *bound, prefetch=batch_size)
                    await asyncio.wait_for(_noop(), 0)  # cooperative point
                    while True:
                        rows = await asyncio.wait_for(cursor.fetch(batch_size), timeout=timeout)
                        if not rows:
                            return
                        yield [dict(r) for r in rows]
            except asyncpg.PostgresError as e:
                raise TargetDBError(f"Target DB error: {e.__class__.__name__}") from e
            except asyncio.TimeoutError as e:
                raise TargetDBError("Target DB query timed out") from e

    async def execute_one(self, sql: str, params: dict[str, Any], *, timeout: float) -> Any:
        if self.db_type in (DBType.postgres, DBType.cloudsql):
            async with self._pool.acquire() as conn:
                return await asyncio.wait_for(conn.fetchval(sql, *params.values()), timeout=timeout)
        raise TargetDBError(f"execute_one not implemented for {self.db_type.value}")

    async def close(self) -> None:
        try:
            await self._pool.close()
        except Exception:  # pragma: no cover
            log.exception("error_closing_target_pool")


async def _noop() -> None:
    return None


class TargetPoolRegistry:
    def __init__(self) -> None:
        s = get_settings()
        self._cache: OrderedDict[uuid.UUID, TargetPool] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max = s.TARGET_POOL_MAX
        self._min_size = s.TARGET_POOL_MIN_SIZE
        self._max_size = s.TARGET_POOL_MAX_SIZE

    async def get_pool(self, conn: DBConnection) -> TargetPool:
        async with self._lock:
            if conn.id in self._cache:
                self._cache.move_to_end(conn.id)
                return self._cache[conn.id]
            pool = await self._build(conn)
            self._cache[conn.id] = pool
            self._cache.move_to_end(conn.id)
            while len(self._cache) > self._max:
                _, evicted = self._cache.popitem(last=False)
                await evicted.close()
            return pool

    async def evict(self, conn_id: uuid.UUID) -> None:
        async with self._lock:
            pool = self._cache.pop(conn_id, None)
        if pool is not None:
            await pool.close()

    async def _build(self, conn: DBConnection) -> TargetPool:
        username = decrypt(conn.encrypted_username)
        password = decrypt(conn.encrypted_password)
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
                    timeout=10,
                    ssl="require" if conn.ssl_enabled else None,
                )
            except (asyncpg.PostgresError, OSError) as e:
                raise PoolExhausted(f"Failed to create pool: {e.__class__.__name__}") from e
            return TargetPool(conn.db_type, pool)
        raise TargetDBError(f"Unsupported db_type {conn.db_type.value}")

    async def close(self) -> None:
        async with self._lock:
            for pool in self._cache.values():
                await pool.close()
            self._cache.clear()


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
