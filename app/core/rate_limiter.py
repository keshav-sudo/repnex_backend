from __future__ import annotations

import time

from app.core.exceptions import RateLimited
from app.core.redis import get_redis

# Atomic token-bucket in Redis.
# KEYS[1] = bucket key
# ARGV    = capacity, refill_per_sec, now_ms, cost
_LUA = """
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])
local now_ms   = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])

local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])
if tokens == nil then tokens = capacity end
if ts == nil then ts = now_ms end

local delta = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + delta * refill)

local allowed = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_ms = math.ceil(((cost - tokens) / refill) * 1000)
end

redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', KEYS[1], math.max(60000, math.ceil(capacity / refill * 1000)))
return {allowed, retry_ms}
"""


class TokenBucket:
    def __init__(self, name: str, per_minute: int, capacity: int | None = None) -> None:
        self.name = name
        self.refill_per_sec = per_minute / 60.0
        self.capacity = capacity or per_minute

    async def consume(self, identity: str, cost: int = 1) -> None:
        r = get_redis()
        key = f"rl:{self.name}:{identity}"
        result = await r.eval(  # type: ignore[no-untyped-call]
            _LUA, 1, key, self.capacity, self.refill_per_sec, int(time.time() * 1000), cost
        )
        allowed, retry_ms = int(result[0]), int(result[1])
        if not allowed:
            raise RateLimited(
                f"Rate limit exceeded for {self.name}",
                retry_after_ms=retry_ms,
            )
