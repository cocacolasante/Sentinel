"""SecurityHook — blocks prompt injection attempts before processing."""

from __future__ import annotations

import logging

from app.hooks.base import BaseHook, HookContext, HookEvent
from app.security.patterns import INJECTION_PATTERNS

logger = logging.getLogger(__name__)


class SecurityHook(BaseHook):
    name   = "security"
    events = [HookEvent.PRE_PROCESS]

    async def handle(self, ctx: HookContext) -> HookContext:
        for pattern in INJECTION_PATTERNS:
            if pattern.search(ctx.message):
                logger.warning(
                    "Injection blocked | session=%s | pattern=%s | msg=%.80s",
                    ctx.session_id, pattern.pattern, ctx.message,
                )
                ctx.metadata["blocked"] = True
                ctx.metadata["blocked_reply"] = (
                    "I noticed something in your message that looks like an attempt to "
                    "manipulate my instructions. I can't process that request. "
                    "If this was unintentional, please rephrase."
                )
                return ctx
        return ctx
