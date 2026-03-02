"""LoggingHook — logs session_id, intent, and latency for every request."""

from __future__ import annotations

import logging
import time

from app.hooks.base import BaseHook, HookContext, HookEvent

logger = logging.getLogger(__name__)

# Store request start times keyed by session_id (simple; not multi-request safe)
_timers: dict[str, float] = {}


class LoggingHook(BaseHook):
    name   = "logging"
    events = [HookEvent.PRE_PROCESS, HookEvent.POST_PROCESS]

    async def handle(self, ctx: HookContext) -> HookContext:
        if ctx.event == HookEvent.PRE_PROCESS:
            _timers[ctx.session_id] = time.monotonic()
            logger.info(
                "REQUEST  | session=%-20s | msg=%.60s",
                ctx.session_id, ctx.message,
            )
        elif ctx.event == HookEvent.POST_PROCESS:
            elapsed = time.monotonic() - _timers.pop(ctx.session_id, time.monotonic())
            logger.info(
                "RESPONSE | session=%-20s | intent=%-16s | agent=%-12s | %.2fs",
                ctx.session_id, ctx.intent, ctx.agent_name, elapsed,
            )
        return ctx
