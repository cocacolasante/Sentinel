"""
RateLimiter — per-session sliding window request throttle.

Uses Redis INCR + EXPIRE for atomic, TTL-backed counters.
Two windows: per-minute (protects against burst loops) and per-hour
(protects against sustained abuse across reconnects).

Called at the top of Dispatcher.process() before any LLM work is done.
"""

from __future__ import annotations

import redis as redis_lib
from loguru import logger

from app.config import get_settings


class RateLimitExceeded(Exception):
    """Raised when a session exceeds its request rate limit."""


class RateLimiter:
    def __init__(self) -> None:
        self._redis: redis_lib.Redis | None = None

    @property
    def _r(self) -> redis_lib.Redis:
        if self._redis is None:
            s = get_settings()
            self._redis = redis_lib.Redis(
                host=s.redis_host,
                port=s.redis_port,
                password=s.redis_password,
                decode_responses=True,
            )
        return self._redis

    def check(self, session_id: str) -> None:
        """
        Raise RateLimitExceeded if this session is over its rate limit.
        Uses INCR + EXPIRE — if key is new (count == 1), set the TTL.
        This gives a fixed window (not truly sliding) but is correct
        and atomic without Lua scripting.
        """
        s = get_settings()

        min_key = f"brain:rate:{session_id}:minute"
        hour_key = f"brain:rate:{session_id}:hour"

        pipe = self._r.pipeline()
        pipe.incr(min_key)
        pipe.incr(hour_key)
        count_min, count_hour = pipe.execute()

        # Set TTL on first increment (EXPIRE is a no-op if key already has TTL)
        if count_min == 1:
            self._r.expire(min_key, 60)
        if count_hour == 1:
            self._r.expire(hour_key, 3_600)

        if count_min > s.rate_limit_per_minute:
            logger.warning("Rate limit (minute) hit | session={} | count={}", session_id, count_min)
            raise RateLimitExceeded(
                f"Too many requests — {count_min} in the last minute "
                f"(limit: {s.rate_limit_per_minute}). Please slow down."
            )

        if count_hour > s.rate_limit_per_hour:
            logger.warning("Rate limit (hour) hit | session={} | count={}", session_id, count_hour)
            raise RateLimitExceeded(
                f"Too many requests — {count_hour} in the last hour (limit: {s.rate_limit_per_hour}). Try again later."
            )


# ── Module-level singleton ────────────────────────────────────────────────────

rate_limiter = RateLimiter()
