"""Per-tenant token buckets and concurrency semaphores for every provider call (rule 3).

When a tenant is over its rate or concurrency for a provider, the job raises arq.Retry(defer=...) and ARQ runs
it again later. Workers never sleep-block. State lives in Redis, so it holds across worker processes.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from arq import Retry

log = structlog.get_logger()


@dataclass(frozen=True)
class Limit:
    per_minute: int
    burst: int
    concurrent: int


LIMITS: dict[str, Limit] = {
    "openai": Limit(per_minute=120, burst=20, concurrent=4),
    "instantly": Limit(per_minute=90, burst=10, concurrent=2),
    "heyreach": Limit(per_minute=60, burst=5, concurrent=2),
    "apollo": Limit(per_minute=50, burst=5, concurrent=2),
    "calendly": Limit(per_minute=60, burst=5, concurrent=2),
    "sendgrid": Limit(per_minute=60, burst=10, concurrent=2),
}

# KEYS[1] bucket hash. ARGV: capacity, refill per ms, now (ms), cost. Returns ms to wait (0 = taken).
_BUCKET = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + (now - ts) * rate)
local wait = 0
if tokens >= cost then
  tokens = tokens - cost
else
  wait = math.ceil((cost - tokens) / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], 3600000)
return wait
"""


class RateLimiter:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @asynccontextmanager
    async def slot(self, tenant_id: UUID, provider: str) -> AsyncIterator[None]:
        """Take one token and one concurrency slot for this tenant + provider, or defer the job."""
        limit = LIMITS[provider]
        bucket = f"gazi:rl:{tenant_id}:{provider}"
        wait_ms = int(
            await self._redis.eval(
                _BUCKET, 1, bucket, limit.burst, limit.per_minute / 60_000, int(time.time() * 1000), 1
            )
        )
        if wait_ms > 0:
            log.info("ratelimit.deferred", provider=provider, wait_ms=wait_ms)
            raise Retry(defer=max(1.0, wait_ms / 1000))

        sem = f"gazi:sem:{tenant_id}:{provider}"
        in_flight = int(await self._redis.incr(sem))
        await self._redis.expire(sem, 300)  # a crashed worker can't hold a slot forever
        if in_flight > limit.concurrent:
            await self._redis.decr(sem)
            log.info("ratelimit.busy", provider=provider, in_flight=in_flight - 1)
            raise Retry(defer=3)
        try:
            yield
        finally:
            await self._redis.decr(sem)
