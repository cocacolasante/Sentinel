"""ChatSkill — no-op default skill for general chat/reasoning/code/writing."""

from app.skills.base import BaseSkill, SkillResult


class ChatSkill(BaseSkill):
    name = "chat"
    description = (
        "General conversation and reasoning: answer questions, help with analysis, brainstorm ideas, "
        "provide explanations, give opinions. Used as the fallback when no specific skill matches. "
        "Use when Anthony asks general questions, wants a conversation, or when no other skill applies. "
        "NOT the right choice when a specific skill exists for the task "
        "(prefer specialized skills for email, code, calendar, etc.)."
    )
    trigger_intents = ["chat"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        return SkillResult(skill_name=self.name)
