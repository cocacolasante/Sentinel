"""
CI/CD skill — manage GitHub Actions pipelines.

Intents:
  cicd_read    — list workflows, view run status/logs, check pipeline health
  cicd_trigger — manually trigger a workflow run
"""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class CICDReadSkill(BaseSkill):
    name = "cicd_read"
    description = (
        "Check CI/CD pipelines: list GitHub Actions workflows, view run status, "
        "see recent runs, check if tests/deploys are passing"
    )
    trigger_intents = ["cicd_read"]
    approval_category = ApprovalCategory.NONE

    def is_available(self) -> bool:
        from app.config import get_settings

        return bool(get_settings().github_token)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.github import GitHubClient

        client = GitHubClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[GitHub not configured — GITHUB_TOKEN missing in .env]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        from app.config import get_settings

        settings = get_settings()
        repo = params.get("repo", settings.github_default_repo)
        action = params.get("action", "list_workflows")

        if not repo:
            return SkillResult(
                context_data="[cicd_read requires a repo (owner/name) or GITHUB_DEFAULT_REPO in .env]",
                skill_name=self.name,
            )

        if action == "list_workflows":
            data = await client._get(f"/repos/{repo}/actions/workflows")
            workflows = [
                {
                    "id": w["id"],
                    "name": w["name"],
                    "state": w["state"],
                    "path": w["path"],
                }
                for w in data.get("workflows", [])
            ]
            return SkillResult(context_data=json.dumps(workflows, indent=2), skill_name=self.name)

        if action == "list_runs":
            workflow_id = params.get("workflow_id", "")
            path = f"/repos/{repo}/actions/runs"
            extra_params: dict = {"per_page": int(params.get("limit", 10))}
            if workflow_id:
                path = f"/repos/{repo}/actions/workflows/{workflow_id}/runs"
            data = await client._get(path, params=extra_params)
            runs = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "status": r["status"],
                    "conclusion": r.get("conclusion"),
                    "branch": r["head_branch"],
                    "created_at": r["created_at"],
                    "html_url": r["html_url"],
                }
                for r in data.get("workflow_runs", [])
            ]
            return SkillResult(context_data=json.dumps(runs, indent=2), skill_name=self.name)

        if action == "get_run":
            run_id = params.get("run_id", "")
            if not run_id:
                return SkillResult(context_data="[get_run requires run_id]", skill_name=self.name)
            data = await client._get(f"/repos/{repo}/actions/runs/{run_id}")
            summary = {
                "id": data.get("id"),
                "name": data.get("name"),
                "status": data.get("status"),
                "conclusion": data.get("conclusion"),
                "branch": data.get("head_branch"),
                "commit": data.get("head_sha", "")[:8],
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "url": data.get("html_url"),
            }
            return SkillResult(context_data=json.dumps(summary, indent=2), skill_name=self.name)

        return SkillResult(
            context_data=f"[Unknown cicd_read action: {action}]",
            skill_name=self.name,
        )


class CICDTriggerSkill(BaseSkill):
    name = "cicd_trigger"
    description = "Trigger a GitHub Actions workflow run manually"
    trigger_intents = ["cicd_trigger"]
    requires_confirmation = True
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.config import get_settings

        return bool(get_settings().github_token)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.config import get_settings

        settings = get_settings()
        repo = params.get("repo", settings.github_default_repo)
        workflow_id = params.get("workflow_id", params.get("workflow_name", ""))
        ref = params.get("ref", "main")
        inputs = params.get("inputs", {})

        if not repo or not workflow_id:
            return SkillResult(
                context_data="[cicd_trigger requires repo and workflow_id/workflow_name]",
                skill_name=self.name,
            )

        pending = {
            "intent": "cicd_trigger",
            "action": "trigger_workflow",
            "params": params,
            "original": original_message,
        }
        context = (
            f"Show the user this pipeline action and ask for confirmation:\n\n"
            f"**Trigger workflow** `{workflow_id}` on `{repo}` (branch: `{ref}`)\n"
            + (f"Inputs: `{json.dumps(inputs)}`\n" if inputs else "")
            + "\nReply **confirm** to trigger or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
