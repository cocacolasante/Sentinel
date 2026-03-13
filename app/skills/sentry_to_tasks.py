"""
Fetch top N Sentry errors by frequency and create approval-required tasks for each
Integration: Requires sentry_read (to list/inspect errors) and task_create (to create tasks with approval_level=3)
"""

from __future__ import annotations
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class SentryToTasksSkill(BaseSkill):
    name              = "sentry_to_tasks"
    description       = "Fetch the top Sentry errors by frequency and create approval-required tasks for each one. Use when Anthony says 'create tasks from Sentry errors', 'triage Sentry issues to task board', 'turn top errors into tasks', or 'batch-create Sentry fix tasks'. NOTE: This skill is not yet implemented — returns a placeholder message. NOT for: reading Sentry errors (use sentry_read) or the automated triage pipeline (that runs via Celery beat schedule)."
    trigger_intents   = ["sentry_errors_create_approval_tasks", "sentry_to_tasks"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        # TODO: implement — Call sentry_read to get error list sorted by frequency, extract top 10, loop through each error and call task_create with title='[Sentry] {error_name}', description='{error details/link}', priority=map_frequency_to_priority, approval_level=3. Could be implemented as an n8n workflow (n8n_manage → create workflow) or as a composite skill that chains sentry_read + task_create.
        return SkillResult(
            context_data="[sentry_to_tasks skill not yet implemented]",
            skill_name=self.name,
        )
