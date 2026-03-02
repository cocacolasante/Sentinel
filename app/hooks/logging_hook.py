"""
LoggingHook — structured Loguru logging + event bus emission at request boundaries.

Fires on PRE_PROCESS (point 1: request_received) and POST_PROCESS (point 4: response_delivered).
Points 2 (llm_called) and 3 (skill_dispatched) are emitted from LLMRouter and Dispatcher directly.
"""

from __future__ import annotations

import time

from loguru import logger

from app.hooks.base import BaseHook, HookContext, HookEvent
from app.observability.event_bus import event_bus

# Timer keyed by session_id. Not perfectly concurrent for same-session parallel
# requests, but acceptable for a single-user brain.
_timers: dict[str, float] = {}


class LoggingHook(BaseHook):
    name   = "logging"
    events = [HookEvent.PRE_PROCESS, HookEvent.POST_PROCESS]

    async def handle(self, ctx: HookContext) -> HookContext:
        if ctx.event == HookEvent.PRE_PROCESS:
            _timers[ctx.session_id] = time.monotonic()

            event = {
                "event":      "request_received",
                "session_id": ctx.session_id,
                "source":     ctx.metadata.get("source", "unknown"),
                "message_preview": ctx.message[:120],
            }
            logger.info(
                "REQUEST  | session={session_id} | src={source} | msg={msg:.80}",
                session_id=ctx.session_id,
                source=ctx.metadata.get("source", "unknown"),
                msg=ctx.message,
            )
            await event_bus.publish(event)

        elif ctx.event == HookEvent.POST_PROCESS:
            start   = _timers.pop(ctx.session_id, time.monotonic())
            elapsed = round((time.monotonic() - start) * 1000, 1)  # ms

            success = not ctx.metadata.get("error")
            event = {
                "event":        "response_delivered",
                "session_id":   ctx.session_id,
                "intent":       ctx.intent,
                "agent":        ctx.agent_name,
                "latency_ms":   elapsed,
                "reply_length": len(ctx.reply),
                "success":      success,
                "error":        ctx.metadata.get("error_message") if not success else None,
            }
            if success:
                logger.info(
                    "RESPONSE | session={session_id} | intent={intent} | agent={agent} | {ms}ms | {chars}c",
                    session_id=ctx.session_id,
                    intent=ctx.intent,
                    agent=ctx.agent_name,
                    ms=elapsed,
                    chars=len(ctx.reply),
                )
            else:
                logger.error(
                    "RESPONSE | session={session_id} | intent={intent} | FAILED | {ms}ms | {err}",
                    session_id=ctx.session_id,
                    intent=ctx.intent,
                    ms=elapsed,
                    err=ctx.metadata.get("error_message", "unknown"),
                )
            await event_bus.publish(event)

        return ctx
