"""N8n skills — trigger workflows and manage workflow definitions."""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class N8nSkill(BaseSkill):
    name = "n8n"
    description = "Run a specific n8n workflow by name"
    trigger_intents = ["n8n_execute"]

    def is_available(self) -> bool:
        from app.integrations.n8n_bridge import N8nBridge
        return N8nBridge().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.n8n_bridge import N8nBridge
        workflow = params.get("workflow", "")
        payload  = params.get("payload", {})
        result   = await N8nBridge().trigger(workflow, payload)
        return SkillResult(context_data=json.dumps(result, indent=2), skill_name=self.name)


class N8nManageSkill(BaseSkill):
    name = "n8n_manage"
    description = (
        "Manage n8n workflows: list all workflows, create a new workflow, "
        "activate/deactivate a workflow, delete a workflow"
    )
    trigger_intents = ["n8n_manage"]
    requires_confirmation = True
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.n8n_bridge import N8nBridge
        return N8nBridge().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.n8n_bridge import N8nBridge
        bridge = N8nBridge()
        action = params.get("action", "list")

        # Read-only
        if action == "list":
            try:
                workflows = await bridge.list_workflows()
                return SkillResult(context_data=json.dumps(workflows, indent=2), skill_name=self.name)
            except Exception as exc:
                return SkillResult(
                    context_data=f"[n8n API error — list_workflows failed: {exc}. "
                                 "Ensure N8N_API_KEY is set in .env for management endpoints.]",
                    skill_name=self.name,
                )

        if action == "get":
            workflow_id = params.get("workflow_id", "")
            if not workflow_id:
                return SkillResult(context_data="[get requires workflow_id]", skill_name=self.name)
            data = await bridge.get_workflow(workflow_id)
            return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)

        # Write actions → confirmation
        pending = {
            "intent":   "n8n_manage",
            "action":   action,
            "params":   params,
            "original": original_message,
        }

        descriptions = {
            "create":     f"Create new n8n workflow: **{params.get('name', '?')}**",
            "activate":   f"Activate workflow `{params.get('workflow_id', '?')}`",
            "deactivate": f"Deactivate workflow `{params.get('workflow_id', '?')}`",
            "delete":     f"**DELETE** workflow `{params.get('workflow_id', '?')}` — this cannot be undone",
        }
        description = descriptions.get(action, f"n8n action: {action}")
        context = (
            f"Show the user this n8n action and ask for confirmation:\n\n"
            f"**{description}**\n\n"
            "Reply **confirm** to proceed or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
