"""
SessionHook — fires on SESSION_START and SESSION_END.

SESSION_START: loads warm summary from Postgres if no hot history exists.
SESSION_END:   flushes the current session to Postgres via MemoryManager.
"""

from __future__ import annotations

import logging

from app.hooks.base import BaseHook, HookContext, HookEvent

logger = logging.getLogger(__name__)


class SessionHook(BaseHook):
    name = "session"
    events = [HookEvent.SESSION_START, HookEvent.SESSION_END]

    async def handle(self, ctx: HookContext) -> HookContext:
        if ctx.event == HookEvent.SESSION_START:
            ctx.metadata["warm_summary_loaded"] = True
            logger.debug("SESSION_START | session=%s", ctx.session_id)

        elif ctx.event == HookEvent.SESSION_END:
            logger.debug("SESSION_END   | session=%s — flushing to Postgres", ctx.session_id)
            try:
                from app.config import get_settings
                from app.memory.memory_manager import MemoryManager

                settings = get_settings()
                mm = MemoryManager(
                    redis_host=settings.redis_host,
                    redis_port=settings.redis_port,
                    redis_password=settings.redis_password,
                    postgres_dsn=settings.postgres_dsn,
                    qdrant_host=settings.qdrant_host,
                    qdrant_port=settings.qdrant_port,
                    qdrant_collection=settings.qdrant_collection,
                )
                await mm.flush_session_to_postgres(ctx.session_id, intent=ctx.intent)
            except Exception as exc:
                logger.error("SessionHook flush failed: %s", exc)

        return ctx
