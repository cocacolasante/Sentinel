"""GitHub skills — read notifications/issues/PRs, create issues, and manage repo monitors."""

from __future__ import annotations

import json

from app.config import get_settings
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()


class GitHubReadSkill(BaseSkill):
    name = "github_read"
    description = (
        "Read GitHub repositories: list repos, view open issues and PRs, read code, search "
        "commits, check branch status, view pull request details. Use when Anthony says "
        "'list GitHub issues', 'show open PRs', 'check GitHub repo', 'what issues are open', "
        "'show me the PR for [branch]', 'read [file] from repo', or 'search issues for [keyword]'. "
        "NOT for: creating issues/PRs (use github_write) or CI/CD (use cicd_read)."
    )
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
    description = (
        "Create and manage GitHub issues, pull requests, and comments: open issues, create PRs, "
        "close issues, add labels, merge PRs. Use when Anthony says 'create GitHub issue', "
        "'open a PR for [branch]', 'close issue #[N]', 'add label to issue', 'merge PR #[N]', "
        "or 'comment on issue #[N]'. Requires CRITICAL approval. "
        "NOT for: reading GitHub (use github_read) or CI/CD pipelines (use cicd_trigger)."
    )
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
        if action == "comment":
            repo = params.get("repo", settings.github_default_repo)
            issue_number = int(params.get("issue_number") or params.get("number", 0))
            body = params.get("body", params.get("text", ""))
            if not issue_number:
                return SkillResult(context_data="[issue_number required for comment action]", skill_name=self.name, is_error=True)
            result = await client.add_issue_comment(repo, issue_number, body)
            return SkillResult(context_data=json.dumps(result, indent=2), skill_name=self.name)
        if action == "close_issue":
            repo = params.get("repo", settings.github_default_repo)
            issue_number = int(params.get("issue_number") or params.get("number", 0))
            if not issue_number:
                return SkillResult(context_data="[issue_number required for close_issue action]", skill_name=self.name, is_error=True)
            import httpx
            headers = {
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            async with httpx.AsyncClient(headers=headers, timeout=15) as http:
                r = await http.patch(
                    f"https://api.github.com/repos/{repo}/issues/{issue_number}",
                    json={"state": "closed"},
                )
                r.raise_for_status()
                data = r.json()
            return SkillResult(
                context_data=json.dumps({"number": data.get("number"), "state": data.get("state"), "url": data.get("html_url")}, indent=2),
                skill_name=self.name,
            )
        return SkillResult(
            context_data=f"[GitHub action '{action}' not yet implemented]",
            skill_name=self.name,
        )


class GitHubMonitorSkill(BaseSkill):
    name = "github_monitor"
    description = (
        "Monitor GitHub for new issues and auto-triage them: fetch open issues, analyze severity "
        "with LLM, classify and prioritize, post findings to Slack. Use when Anthony says "
        "'monitor GitHub issues', 'triage new issues', 'check for new bug reports', "
        "'auto-label GitHub issues', or 'summarize recent issues'. "
        "NOT for: reading specific issues (use github_read) or creating issues (use github_write)."
    )
    trigger_intents = ["github_monitor"]
    approval_category = ApprovalCategory.STANDARD

    def is_available(self) -> bool:
        return True  # Always available — uses local DB

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.db import postgres

        action = params.get("action", "list")

        if action == "list":
            rows = postgres.execute(
                """
                SELECT g.id, g.repo, g.label, g.enabled, g.agent_id,
                       g.issue_filter, g.last_polled_at,
                       m.app_name AS agent_name
                FROM github_repo_monitors g
                LEFT JOIN mesh_agents m ON g.agent_id = m.agent_id
                ORDER BY g.repo
                """
            )
            if not rows:
                return SkillResult(
                    context_data="No GitHub repo monitors configured yet. Use action=add to add one.",
                    skill_name=self.name,
                )
            lines = ["**GitHub Repo Monitors:**\n"]
            for r in rows:
                agent_label = f" → agent `{r['agent_name']}`" if r.get("agent_name") else ""
                status = "✅" if r["enabled"] else "⏸"
                last = str(r["last_polled_at"])[:16] if r.get("last_polled_at") else "never"
                lines.append(f"{status} `{r['repo']}` (id={r['id']}) | last polled: {last}{agent_label}")
            return SkillResult(context_data="\n".join(lines), skill_name=self.name)

        if action == "add":
            repo = params.get("repo", "")
            if not repo:
                return SkillResult(
                    context_data="[repo parameter required for action=add, e.g. 'owner/repo']",
                    skill_name=self.name,
                    is_error=True,
                )
            label = params.get("label", "")
            agent_id = params.get("agent_id") or None
            issue_filter = params.get("issue_filter", "is:open is:issue")
            poll_labels = params.get("poll_labels") or None
            try:
                row = postgres.execute_one(
                    """
                    INSERT INTO github_repo_monitors
                        (repo, label, enabled, agent_id, issue_filter, poll_labels)
                    VALUES (%s, %s, TRUE, %s::uuid, %s, %s)
                    ON CONFLICT (repo) DO UPDATE SET
                        label = EXCLUDED.label,
                        enabled = TRUE,
                        agent_id = EXCLUDED.agent_id,
                        issue_filter = EXCLUDED.issue_filter,
                        poll_labels = EXCLUDED.poll_labels,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (repo, label or None, agent_id, issue_filter, poll_labels),
                )
                return SkillResult(
                    context_data=f"✅ Now monitoring `{repo}` (id={row['id']}). Brain will poll for open issues every 30 min.",
                    skill_name=self.name,
                )
            except Exception as exc:
                return SkillResult(context_data=f"[Failed to add monitor: {exc}]", skill_name=self.name, is_error=True)

        if action == "remove":
            repo = params.get("repo", "")
            if not repo:
                return SkillResult(context_data="[repo required for action=remove]", skill_name=self.name, is_error=True)
            postgres.execute("DELETE FROM github_repo_monitors WHERE repo = %s", (repo,))
            return SkillResult(context_data=f"✅ Removed monitor for `{repo}`.", skill_name=self.name)

        if action in ("enable", "disable"):
            repo = params.get("repo", "")
            if not repo:
                return SkillResult(context_data=f"[repo required for action={action}]", skill_name=self.name, is_error=True)
            enabled = action == "enable"
            postgres.execute(
                "UPDATE github_repo_monitors SET enabled = %s, updated_at = NOW() WHERE repo = %s",
                (enabled, repo),
            )
            word = "enabled" if enabled else "paused"
            return SkillResult(context_data=f"✅ Monitor for `{repo}` {word}.", skill_name=self.name)

        if action == "assign":
            repo = params.get("repo", "")
            agent_id = params.get("agent_id", "")
            if not repo or not agent_id:
                return SkillResult(
                    context_data="[Both repo and agent_id required for action=assign]",
                    skill_name=self.name,
                    is_error=True,
                )
            # Verify agent exists
            agent = postgres.execute_one(
                "SELECT app_name FROM mesh_agents WHERE agent_id = %s::uuid AND is_revoked = FALSE",
                (agent_id,),
            )
            if not agent:
                return SkillResult(
                    context_data=f"[Agent `{agent_id}` not found or revoked]",
                    skill_name=self.name,
                    is_error=True,
                )
            postgres.execute(
                "UPDATE github_repo_monitors SET agent_id = %s::uuid, updated_at = NOW() WHERE repo = %s",
                (agent_id, repo),
            )
            return SkillResult(
                context_data=f"✅ Repo `{repo}` assigned to agent `{agent['app_name']}` (`{agent_id[:8]}...`). Patches for this repo will be dispatched to that agent.",
                skill_name=self.name,
            )

        return SkillResult(
            context_data=f"[Unknown action '{action}'. Use: add, remove, list, enable, disable, assign]",
            skill_name=self.name,
            is_error=True,
        )
