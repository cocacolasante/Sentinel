"""ChatSkill — no-op default skill for general chat/reasoning/code/writing."""

from app.skills.base import BaseSkill, SkillResult


class ChatSkill(BaseSkill):
    name = "chat"
    description = "General chat, reasoning, coding, and writing (no external action)"
    trigger_intents = ["chat"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        return SkillResult(skill_name=self.name)
