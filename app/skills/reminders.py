"""
Create, manage, and track reminders and to-do items with notifications and persistence
Integration: Google Tasks API (native reminders/to-do service), or Google Calendar notifications + custom metadata. Google Tasks is preferred as it's purpose-built for to-dos.
"""

from __future__ import annotations
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class RemindersSkill(BaseSkill):
    name = "reminders"
    description = "Create and manage reminders and to-do items via Google Tasks: set reminders with due dates, list pending reminders, mark done, snooze. Use when Anthony says 'remind me to', 'set a reminder for', 'add to my to-do list', 'create a task for', 'what reminders do I have', or 'show my to-dos'. NOTE: This skill is not yet implemented — returns a placeholder message. NOT for: Google Calendar events (use calendar_write) or task board items (use task_create)."
    trigger_intents = ["set_reminder", "create_todo", "list_reminders", "update_reminder", "dismiss_reminder", "snooze_reminder"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        # TODO: implement — Integrate with Google Tasks API (tasks.googleapis.com). Support: create task/reminder with title+description+due_date+priority, list tasks (all/by status), update task status (pending/completed), set reminder notifications (1hr/1day before due), snooze/dismiss, and filter by list. Use OAuth scope 'https://www.googleapis.com/auth/tasks'. Leverage existing Gmail auth if already configured.
        return SkillResult(
            context_data="[reminders skill not yet implemented]",
            skill_name=self.name,
        )
