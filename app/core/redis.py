from __future__ import annotations

import logging

import redis.asyncio as aioredis
from app.core.config import get_settings

log = logging.getLogger(__name__)

_client: aioredis.Redis | None = None
_redis_available: bool = False


async def init_redis() -> aioredis.Redis | None:
    global _client, _redis_available
    settings = get_settings()

    if not settings.REDIS_URL:
        log.warning("REDIS_URL not set — Redis disabled. Rate limiting will be skipped.")
        _redis_available = False
        return None

    try:
        _client = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True, health_check_interval=30
        )
        await _client.ping()
        _redis_available = True
        log.info("Redis connected successfully.")
        return _client
    except Exception as exc:
        log.warning(
            f"Redis unavailable ({exc}). Starting without Redis — rate limiting disabled."
        )
        _client = None
        _redis_available = False
        return None


async def close_redis() -> None:
    global _client, _redis_available
    if _client is not None:
        await _client.aclose()
        _client = None
    _redis_available = False


def get_redis() -> aioredis.Redis | None:
    return _client


def is_redis_available() -> bool:
    return _redis_available
