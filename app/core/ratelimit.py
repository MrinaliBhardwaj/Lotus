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
    def __init__(self, redis_url: str, retry_after_seconds: float = 30.0) -> None:
        self._redis_url = redis_url
        self._retry_after = retry_after_seconds
        self._redis: aioredis.Redis | None = None
        # 0.0 means "not degraded"; otherwise the monotonic time to retry Redis.
        # Time-boxed, so a transient blip doesn't latch us to per-process memory
        # for the life of the process (M1) — limits self-heal across replicas.
        self._redis_retry_at = 0.0
        self._memory: dict[str, int] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> None:
        """Count one request against ``key``; raise once the window's limit is hit."""
        window = int(time.time()) // window_seconds
        bucket = f"ratelimit:{key}:{window}"
        if await self._incr(bucket, ttl=window_seconds * 2) > limit:
            raise RateLimitedError("rate limit exceeded — retry shortly")

    async def _incr(self, bucket: str, *, ttl: int) -> int:
        if self._redis_retry_at <= time.monotonic():
            try:
                if self._redis is None:
                    self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                        self._redis_url, socket_connect_timeout=1, socket_timeout=1
                    )
                count = int(await self._redis.incr(bucket))
                if count == 1:
                    await self._redis.expire(bucket, ttl)
                self._redis_retry_at = 0.0  # recovered
                return count
            except (aioredis.RedisError, OSError):
                self._redis = None  # drop the dead client; rebuild on retry
                self._redis_retry_at = time.monotonic() + self._retry_after
                logger.warning(
                    "redis unreachable — rate limiting on per-process memory for %.0fs",
                    self._retry_after,
                )
        if len(self._memory) > _MEMORY_MAX_KEYS:  # old windows never get hit again; drop them
            self._memory.clear()
        self._memory[bucket] = self._memory.get(bucket, 0) + 1
        return self._memory[bucket]


@lru_cache
def get_limiter() -> FixedWindowLimiter:
    settings = get_settings()
    return FixedWindowLimiter(
        str(settings.redis_url), retry_after_seconds=settings.rate_limit_redis_retry_seconds
    )


class SyncTokenBucket:
    """Worker-side token bucket for outbound provider calls (Task 6 pin):
    smooths embedding request bursts under the vendor's rate limit."""

    def __init__(self, rate_per_minute: int, capacity: int | None = None) -> None:
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(capacity if capacity is not None else max(rate_per_minute // 10, 1))
        self._tokens = self._capacity
        self._updated = time.monotonic()

    def acquire(self, tokens: int = 1) -> None:
        """Block until ``tokens`` are available, then consume them."""
        while True:
            now = time.monotonic()
            self._tokens = min(
                self._capacity, self._tokens + (now - self._updated) * self._rate_per_second
            )
            self._updated = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return
            time.sleep((tokens - self._tokens) / self._rate_per_second)
