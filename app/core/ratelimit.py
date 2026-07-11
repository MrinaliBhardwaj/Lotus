"""Fixed-window rate limiting (CLAUDE.md §2.1 #11).

Redis-backed so limits hold across API replicas. If Redis is unreachable
(unit tests, keyless local dev) the limiter degrades to a per-process
in-memory window — fail-open by design: losing Redis must never take the
API down with it.
"""

import logging
import time
from functools import lru_cache

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError

logger = logging.getLogger(__name__)

_MEMORY_MAX_KEYS = 4096


class FixedWindowLimiter:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None
        self._redis_broken = False
        self._memory: dict[str, int] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> None:
        """Count one request against ``key``; raise once the window's limit is hit."""
        window = int(time.time()) // window_seconds
        bucket = f"ratelimit:{key}:{window}"
        if await self._incr(bucket, ttl=window_seconds * 2) > limit:
            raise RateLimitedError("rate limit exceeded — retry shortly")

    async def _incr(self, bucket: str, *, ttl: int) -> int:
        if not self._redis_broken:
            try:
                if self._redis is None:
                    self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                        self._redis_url, socket_connect_timeout=1, socket_timeout=1
                    )
                count = int(await self._redis.incr(bucket))
                if count == 1:
                    await self._redis.expire(bucket, ttl)
                return count
            except (aioredis.RedisError, OSError):
                self._redis_broken = True
                logger.warning("redis unreachable — rate limiting degraded to per-process memory")
        if len(self._memory) > _MEMORY_MAX_KEYS:  # old windows never get hit again; drop them
            self._memory.clear()
        self._memory[bucket] = self._memory.get(bucket, 0) + 1
        return self._memory[bucket]


@lru_cache
def get_limiter() -> FixedWindowLimiter:
    return FixedWindowLimiter(str(get_settings().redis_url))
