"""
CI/CD skill — manage GitHub Actions pipelines.

Intents:
  cicd_read    — list workflows, view run status/logs, check pipeline health
  cicd_trigger — manually trigger a workflow run
"""

from __future__ import annotations

import json
import logging

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)


class CICDReadSkill(BaseSkill):
    name = "cicd_read"
    description = (
        "Check CI/CD pipeline status via GitHub Actions: list workflows, view recent runs, "
        "check pass/fail status, see logs for failed steps. Use when Anthony says 'check CI', "
        "'is the pipeline passing', 'list GitHub Actions', 'show recent runs', 'did the tests pass', "
        "or 'why did CI fail'. NOT for: triggering/running pipelines (use cicd_trigger) or "
        "debugging failed runs in detail (use cicd_debug)."
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
            try:
                data = await client._get(f"/repos/{repo}/actions/workflows")
            except Exception as exc:
                logger.exception("CICDReadSkill list_workflows repo=%s: %s", repo, exc)
                return SkillResult(
                    context_data=f"[CICD error listing workflows for '{repo}': {exc}]",
                    skill_name=self.name,
                    is_error=True,
                )
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
            try:
                data = await client._get(path, params=extra_params)
            except Exception as exc:
                logger.exception("CICDReadSkill list_runs repo=%s workflow_id=%s: %s", repo, workflow_id, exc)
                return SkillResult(
                    context_data=f"[CICD error listing runs for '{repo}': {exc}]",
                    skill_name=self.name,
                    is_error=True,
                )
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
            try:
                data = await client._get(f"/repos/{repo}/actions/runs/{run_id}")
            except Exception as exc:
                logger.exception("CICDReadSkill get_run repo=%s run_id=%s: %s", repo, run_id, exc)
                return SkillResult(
                    context_data=f"[CICD error fetching run '{run_id}' for '{repo}': {exc}]",
                    skill_name=self.name,
                    is_error=True,
                )
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
    description = (
        "Manually trigger a GitHub Actions workflow run. Use when Anthony says 'trigger workflow', "
        "'run CI', 'kick off pipeline', 'deploy via GitHub Actions', or 'run [workflow name]'. "
        "Requires confirmation. NOT for: checking pipeline status (use cicd_read) or "
        "debugging failures (use cicd_debug)."
    )
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
