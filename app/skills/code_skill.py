"""CodeSkill — routing hint for code tasks (Sonnet + extended tokens)."""

from __future__ import annotations

from app.skills.base import BaseSkill, SkillResult


class CodeSkill(BaseSkill):
    name = "code"
    description = "Software engineering, code review, debugging, and architecture"
    trigger_intents = ["code"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        # No external data needed — just a routing signal for the agent selector
        return SkillResult(skill_name=self.name)
