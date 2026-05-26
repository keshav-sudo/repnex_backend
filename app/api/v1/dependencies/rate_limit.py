from __future__ import annotations

from fastapi import Depends

from app.api.v1.dependencies.auth import get_current_user
from app.core.config import get_settings
from app.core.rate_limiter import TokenBucket
from app.core.security.auth import CurrentUser

_buckets: dict[str, TokenBucket] = {}


def _bucket_for(name: str) -> TokenBucket:
    if name not in _buckets:
        s = get_settings()
        per_min = {
            "auth": s.RATE_LIMIT_AUTH_PER_MIN,
            "query": s.RATE_LIMIT_QUERY_PER_MIN,
            "api": s.RATE_LIMIT_API_PER_MIN,
            "ws_msg": s.WS_MSG_PER_MIN,
        }[name]
        _buckets[name] = TokenBucket(name, per_minute=per_min)
    return _buckets[name]


def rate_limit(name: str):
    bucket = _bucket_for(name)

    async def _dep(current: CurrentUser = Depends(get_current_user)) -> None:
        await bucket.consume(str(current.user_id))

    return _dep


async def consume_ws_msg(user_id: str) -> None:
    await _bucket_for("ws_msg").consume(user_id)


async def consume_anon(name: str, identity: str) -> None:
    await _bucket_for(name).consume(identity)
