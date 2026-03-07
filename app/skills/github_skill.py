"""GitHub skills — read notifications/issues/PRs and create issues."""

from __future__ import annotations

import json

from app.config import get_settings
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()


class GitHubReadSkill(BaseSkill):
    name = "github_read"
    description = "Check GitHub issues, PRs, notifications, or repo info"
    trigger_intents = ["github_read"]

    def is_available(self) -> bool:
        from app.integrations.github import GitHubClient

        return GitHubClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.github import GitHubClient

        client = GitHubClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[GitHub not configured — GITHUB_TOKEN missing]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )
        resource = params.get("resource", "notifications")
        repo = params.get("repo", "")
        if resource == "issues":
            data = await client.list_issues(repo)
        elif resource == "prs":
            data = await client.list_prs(repo)
        else:
            data = await client.list_notifications()
        return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)


class GitHubWriteSkill(BaseSkill):
    name = "github_write"
    description = "Create a GitHub issue, comment on a PR, or close an issue"
    trigger_intents = ["github_write"]
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.github import GitHubClient

        return GitHubClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.github import GitHubClient

        client = GitHubClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[GitHub not configured]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )
        action = params.get("action", "create_issue")
        if action == "create_issue":
            result = await client.create_issue(
                repo=params.get("repo", settings.github_default_repo),
                title=params.get("title", "New issue"),
                body=params.get("body", ""),
            )
            return SkillResult(context_data=json.dumps(result, indent=2), skill_name=self.name)
        return SkillResult(
            context_data=f"[GitHub action '{action}' not yet implemented]",
            skill_name=self.name,
        )
