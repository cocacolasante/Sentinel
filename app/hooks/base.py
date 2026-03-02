"""
Hook system base types.

HookEvent  — lifecycle events a hook can subscribe to
HookContext — mutable context object passed through the hook chain
BaseHook   — abstract base class for all hooks
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class HookEvent(str, Enum):
    PRE_PROCESS   = "pre_process"
    POST_PROCESS  = "post_process"
    SESSION_START = "session_start"
    SESSION_END   = "session_end"
    SKILL_START   = "skill_start"
    SKILL_END     = "skill_end"


@dataclass
class HookContext:
    session_id: str  = ""
    message:    str  = ""
    reply:      str  = ""
    intent:     str  = ""
    agent_name: str  = "default"
    event:      HookEvent = HookEvent.PRE_PROCESS
    metadata:   dict = field(default_factory=dict)


class BaseHook(ABC):
    """Abstract base class for all lifecycle hooks."""

    name:   str             = "base"
    events: list[HookEvent] = []

    @abstractmethod
    async def handle(self, ctx: HookContext) -> HookContext:
        """Process the context and return it (possibly modified)."""
        ...
