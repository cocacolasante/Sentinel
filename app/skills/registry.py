"""
SkillRegistry — maps intents to skill instances.

Skills register themselves via register(). The dispatcher uses get(intent) to
look up the right skill, falling back to ChatSkill for unrecognized intents.
"""

from __future__ import annotations

import logging

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}
        self._all: list[BaseSkill] = []

    def register(self, skill: BaseSkill) -> None:
        """Register a skill for all its trigger intents."""
        self._all.append(skill)
        for intent in skill.trigger_intents:
            if intent in self._skills:
                logger.warning(
                    "Intent '%s' already registered by %s — overwriting with %s",
                    intent,
                    self._skills[intent].name,
                    skill.name,
                )
            self._skills[intent] = skill

    def get(self, intent: str) -> BaseSkill:
        """Return the skill for the given intent, or ChatSkill as fallback."""
        from app.skills.chat_skill import ChatSkill

        return self._skills.get(intent, ChatSkill())

    def list_available(self) -> list[BaseSkill]:
        """Return skills that are currently configured and available."""
        return [s for s in self._all if s.is_available()]

    def list_all_descriptions(self) -> str:
        """
        Return a formatted string of all skill intents + descriptions.
        Injected into the intent classifier prompt so the list is registry-driven.
        """
        lines: list[str] = []
        for skill in self._all:
            intents = ", ".join(skill.trigger_intents) if skill.trigger_intents else "(fallback)"
            available = "" if skill.is_available() else " [unavailable]"
            lines.append(f"{intents}{available}  — {skill.description}")
        return "\n".join(lines)
