"""CodeSkill — routing hint for code tasks (Sonnet + extended tokens)."""

from __future__ import annotations

from app.skills.base import BaseSkill, SkillResult


class CodeSkill(BaseSkill):
    name = "code"
    description = (
        "Write, explain, review, or debug code in any language: Python, JavaScript/TypeScript, "
        "Go, Rust, SQL, bash, and more. Use when Anthony says 'write a function', "
        "'explain this code', 'debug this', 'how do I [coding task]', 'review this code', "
        "'refactor this', or 'write a script to'. "
        "NOT for: making changes to files in a repo (use repo_write), running code (use server_shell), "
        "or building full projects (use se_workflow)."
    )
    trigger_intents = ["code"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        # No external data needed — just a routing signal for the agent selector
        return SkillResult(skill_name=self.name)
