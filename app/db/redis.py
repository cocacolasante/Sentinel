"""
Shared async Redis client for Sentinel.

Usage:
    from app.db.redis import get_redis
    redis = await get_redis()
    await redis.set("key", "value")
"""
from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return a shared async Redis client (lazy-initialised)."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = aioredis.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    """Close and reset the shared client (used in tests / shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
