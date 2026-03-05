"""
Sentry Skills

SentryReadSkill   — list and inspect Sentry issues
SentryManageSkill — resolve, ignore, assign, comment on issues
"""

from __future__ import annotations

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

_LEVEL_BADGE = {
    "fatal": "🔴",
    "critical": "🔴",
    "error": "🟠",
    "warning": "🟡",
    "info": "🔵",
    "debug": "⚪",
}


class SentryReadSkill(BaseSkill):
    name = "sentry_read"
    description = "List and inspect Sentry error issues"
    trigger_intents = ["sentry_read"]

    def is_available(self) -> bool:
        from app.integrations.sentry_client import SentryClient

        return SentryClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.sentry_client import SentryClient

        client = SentryClient()
        action = params.get("action", "list")
        project = params.get("project")
        query = params.get("query", "is:unresolved")
        limit = int(params.get("limit", 20))

        if action == "list":
            issues = await client.list_issues(project=project, query=query, limit=limit)
            if not issues:
                return SkillResult(data={"issues": []}, summary="No Sentry issues found matching that query.")
            lines = [f"**Sentry Issues** ({len(issues)} found)\n"]
            for i in issues:
                badge = _LEVEL_BADGE.get(i["level"], "⚪")
                last = i["last_seen"][:10] if i.get("last_seen") else "N/A"
                lines.append(f"{badge} `{i['id']}` [{i['level'].upper()}] **{i['title']}**")
                lines.append(f"   Project: {i['project']} | Count: {i['count']} | Last seen: {last}")
                if i.get("permalink"):
                    lines.append(f"   {i['permalink']}")
            return SkillResult(data={"issues": issues}, summary="\n".join(lines))

        if action == "get":
            issue_id = params.get("issue_id", "")
            if not issue_id:
                return SkillResult(data={}, summary="Please provide an issue_id to look up.")
            issue = await client.get_issue(issue_id)
            badge = _LEVEL_BADGE.get(issue["level"], "⚪")
            first = issue["first_seen"][:10] if issue.get("first_seen") else "N/A"
            lines = [
                f"{badge} **{issue['title']}**",
                f"ID: `{issue['id']}` | Level: {issue['level']} | Status: {issue['status']}",
                f"Project: {issue['project']} | Platform: {issue['platform']}",
                f"Count: {issue['count']} | First seen: {first}",
            ]
            if issue.get("culprit"):
                lines.append(f"Culprit: {issue['culprit']}")
            if issue.get("assigned_to"):
                lines.append(f"Assigned to: {issue['assigned_to']}")
            if issue.get("permalink"):
                lines.append(f"Link: {issue['permalink']}")
            return SkillResult(data=issue, summary="\n".join(lines))

        # list_from_db — show recently received issues stored in Postgres
        if action == "db":
            try:
                from app.db import postgres

                rows = postgres.execute(
                    """
                    SELECT issue_id, title, level, status, project, count, category, received_at
                    FROM   sentry_issues
                    ORDER  BY received_at DESC
                    LIMIT  %s
                    """,
                    (limit,),
                )
                if not rows:
                    return SkillResult(data={"issues": []}, summary="No Sentry issues tracked yet.")
                lines = [f"**Tracked Sentry Issues** ({len(rows)} recent)\n"]
                for r in rows:
                    badge = _LEVEL_BADGE.get(r["level"], "⚪")
                    ts = r["received_at"].strftime("%Y-%m-%d") if r.get("received_at") else "N/A"
                    lines.append(f"{badge} `{r['issue_id']}` [{r['level'].upper()}] **{r['title']}**")
                    lines.append(f"   Project: {r['project']} | Count: {r['count']} | Category: {r['category']} | {ts}")
                return SkillResult(data={"issues": rows}, summary="\n".join(lines))
            except Exception as exc:
                return SkillResult(data={}, summary=f"Could not load tracked issues: {exc}")

        return SkillResult(data={}, summary=f"Unknown action: `{action}`. Use 'list', 'get', or 'db'.")


class SentryManageSkill(BaseSkill):
    name = "sentry_manage"
    description = "Resolve, ignore, assign, or comment on Sentry issues"
    trigger_intents = ["sentry_manage"]
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.sentry_client import SentryClient

        return SentryClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "")
        issue_id = params.get("issue_id", "")

        if not issue_id:
            return SkillResult(data={}, summary="Please provide the Sentry `issue_id` to act on.")
        if not action:
            return SkillResult(data={}, summary="Please specify an action: resolve, ignore, assign, or comment.")

        # Lower category for non-destructive ops
        if action in ("assign", "comment"):
            self.approval_category = ApprovalCategory.STANDARD

        labels = {
            "resolve": f"Resolve Sentry issue `{issue_id}`",
            "ignore": f"Ignore Sentry issue `{issue_id}`",
            "assign": f"Assign Sentry issue `{issue_id}` to **{params.get('assignee', '?')}**",
            "comment": f'Add comment to Sentry issue `{issue_id}`: "{params.get("text", "")[:80]}"',
        }
        label = labels.get(action, f"Perform `{action}` on Sentry issue `{issue_id}`")

        return SkillResult(
            data={},
            summary=label,
            pending_action={
                "action": f"sentry_{action}",
                "params": params,
                "original": original_message,
            },
        )
