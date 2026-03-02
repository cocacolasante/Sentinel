"""N8nSkill — trigger named n8n workflows."""

from __future__ import annotations

import json

from app.skills.base import BaseSkill, SkillResult


class N8nSkill(BaseSkill):
    name = "n8n"
    description = "Run a specific n8n workflow by name"
    trigger_intents = ["n8n_execute"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.n8n_bridge import N8nBridge
        workflow = params.get("workflow", "")
        payload  = params.get("payload", {})
        result   = await N8nBridge().trigger(workflow, payload)
        return SkillResult(context_data=json.dumps(result, indent=2), skill_name=self.name)
