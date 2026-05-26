from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import get_settings

_client: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    global _client
    settings = get_settings()
    _client = aioredis.from_url(
        settings.REDIS_URL, encoding="utf-8", decode_responses=True, health_check_interval=30
    )
    await _client.ping()
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_redis() -> aioredis.Redis:
    if _client is None:
        raise RuntimeError("Redis not initialized")
    return _client
