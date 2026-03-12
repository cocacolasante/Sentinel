"""Reddit router — expose schedule data stored in Redis over REST."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()

_REDIS_SCHEDULES_KEY = "sentinel:reddit:schedules"


@router.get("/reddit/schedules")
async def list_reddit_schedules() -> dict:
    """Return all Reddit digest schedules stored in Redis."""
    try:
        from app.memory.redis_client import RedisMemory

        r = RedisMemory().client
        raw = r.get(_REDIS_SCHEDULES_KEY)
        schedules: list[dict] = json.loads(raw) if raw else []
    except Exception as exc:
        logger.warning("Could not load Reddit schedules: %s", exc)
        schedules = []

    active = [s for s in schedules if s.get("enabled", True)]
    return {
        "count": len(schedules),
        "active_count": len(active),
        "schedules": schedules,
    }
