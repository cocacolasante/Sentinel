"""HookRegistry — manages and fires lifecycle hooks as a middleware chain."""

from __future__ import annotations

import logging

from app.hooks.base import BaseHook, HookContext, HookEvent

logger = logging.getLogger(__name__)


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[BaseHook]] = {}

    def register(self, hook: BaseHook) -> None:
        """Register a hook for all its declared events."""
        for event in hook.events:
            self._hooks.setdefault(event, []).append(hook)

    async def fire(self, event: HookEvent, ctx: HookContext) -> HookContext:
        """
        Run all hooks registered for `event` in registration order.
        Each hook receives (and may modify) the context. If a hook sets
        ctx.metadata["blocked"] = True, subsequent hooks are still run
        to allow logging, but the caller is responsible for checking the flag.
        """
        ctx.event = event
        for hook in self._hooks.get(event, []):
            try:
                ctx = await hook.handle(ctx)
            except Exception as exc:
                logger.error("Hook %s raised on %s: %s", hook.name, event, exc)
        return ctx
