"""
Task Skills

TaskCreateSkill — create a task with priority (1–5) and approval level (1–3)
TaskReadSkill   — list, search, and view tasks
TaskUpdateSkill — update a task's status, priority, or approval level
"""

from __future__ import annotations

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

_PRIORITY_LABEL  = {
    1: "🟢 Low (1)",
    2: "🔵 Minor (2)",
    3: "🟡 Normal (3)",
    4: "🟠 High (4)",
    5: "🔴 Critical (5)",
}
_APPROVAL_LABEL  = {
    1: "auto-approve",
    2: "needs review",
    3: "requires sign-off",
}
_STATUS_EMOJI = {
    "pending":     "⏳",
    "in_progress": "🔄",
    "done":        "✅",
    "cancelled":   "❌",
}
_PRIORITY_TO_TEXT = {1: "low", 2: "low", 3: "normal", 4: "high", 5: "urgent"}
_TEXT_TO_PRIORITY = {"low": 1, "normal": 3, "high": 4, "urgent": 5}


def _fmt_task(row: dict) -> str:
    pri  = row.get("priority_num") or _TEXT_TO_PRIORITY.get(row.get("priority", "normal"), 3)
    app  = row.get("approval_level", 2)
    stat = row.get("status", "pending")
    line = (
        f"{_STATUS_EMOJI.get(stat, '?')} **#{row['id']}** {row['title']}\n"
        f"   Priority: {_PRIORITY_LABEL.get(pri, str(pri))} | "
        f"Approval: {_APPROVAL_LABEL.get(app, str(app))} | "
        f"Status: {stat}"
    )
    if row.get("tags"):
        line += f" | Tags: {row['tags']}"
    if row.get("due_date"):
        line += f" | Due: {str(row['due_date'])[:10]}"
    if row.get("assigned_to"):
        line += f" | Assigned: {row['assigned_to']}"
    return line


class TaskCreateSkill(BaseSkill):
    name            = "task_create"
    description     = (
        "Create a new task with a title, optional description, priority 1–5 "
        "(1=low, 5=critical), and approval level 1–3 (1=auto-approve, 3=requires sign-off). "
        "Use this when the user asks to track, create, or add a task."
    )
    trigger_intents = ["task_create"]
    approval_category = ApprovalCategory.NONE  # DB insert — no external side-effects

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.db import postgres

        title          = (params.get("title") or "").strip()
        description    = params.get("description", "")
        priority_num   = max(1, min(5, int(params.get("priority", 3))))
        approval_level = max(1, min(3, int(params.get("approval_level", 2))))
        due_date       = params.get("due_date") or None
        source         = params.get("source", "brain")
        tags           = params.get("tags") or None
        assigned_to    = params.get("assigned_to") or None

        if not title:
            return SkillResult(
                context_data=(
                    "[task_create requires a task title. "
                    "Ask the user: what should the task be called?]"
                ),
                skill_name=self.name,
            )

        priority_text = _PRIORITY_TO_TEXT[priority_num]

        try:
            row = postgres.execute_one(
                """
                INSERT INTO tasks
                    (title, description, status, priority, priority_num, approval_level,
                     due_date, source, tags, assigned_to)
                VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, title, status, priority, priority_num, approval_level,
                          due_date, source, tags, assigned_to, created_at
                """,
                (
                    title, description, priority_text, priority_num, approval_level,
                    due_date, source, tags, assigned_to,
                ),
            )
            context = (
                "Task created successfully!\n\n"
                + _fmt_task(row)
                + f"\n\nTask ID: #{row['id']} | Created: {str(row['created_at'])[:19]}"
            )
        except Exception as exc:
            context = f"[task_create failed: {exc}]"

        return SkillResult(context_data=context, skill_name=self.name)


class TaskReadSkill(BaseSkill):
    name            = "task_read"
    description     = (
        "List, filter, or view tasks. Filter by status, priority, or ID. "
        "Use this when the user asks to see, list, show, or check tasks."
    )
    trigger_intents = ["task_read"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.db import postgres

        action   = params.get("action", "list")
        task_id  = params.get("id") or params.get("task_id")
        status   = params.get("status")
        priority = params.get("priority")
        limit    = max(1, min(100, int(params.get("limit", 20))))

        # Single task lookup
        if action == "get" and task_id:
            row = postgres.execute_one(
                """
                SELECT id, title, description, status, priority, priority_num,
                       approval_level, due_date, source, tags, assigned_to,
                       created_at, updated_at
                FROM   tasks WHERE id = %s
                """,
                (int(task_id),),
            )
            if not row:
                return SkillResult(
                    context_data=f"[No task found with ID #{task_id}]",
                    skill_name=self.name,
                )
            context = _fmt_task(row)
            if row.get("description"):
                context += f"\n\nDescription: {row['description']}"
            return SkillResult(context_data=context, skill_name=self.name)

        # Filtered list
        conditions: list[str] = []
        values: list = []

        if status:
            conditions.append("status = %s")
            values.append(status)
        if priority is not None:
            try:
                pri_num = int(priority)
                conditions.append("priority_num = %s")
                values.append(pri_num)
            except (TypeError, ValueError):
                conditions.append("priority = %s")
                values.append(str(priority))

        where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        values.append(limit)

        rows = postgres.execute(
            f"""
            SELECT id, title, status, priority, priority_num, approval_level,
                   due_date, tags, assigned_to, created_at
            FROM   tasks
            {where}
            ORDER BY
                CASE status
                    WHEN 'in_progress' THEN 1
                    WHEN 'pending'     THEN 2
                    ELSE 3
                END,
                COALESCE(priority_num, 3) DESC,
                created_at DESC
            LIMIT %s
            """,
            values,
        )

        if not rows:
            context = "[No tasks found matching your criteria.]"
        else:
            lines = [f"**Tasks** ({len(rows)} found)\n"]
            for r in rows:
                lines.append(_fmt_task(r))
            context = "\n".join(lines)

        return SkillResult(context_data=context, skill_name=self.name)


class TaskUpdateSkill(BaseSkill):
    name            = "task_update"
    description     = (
        "Update a task — change its status (pending/in_progress/done/cancelled), "
        "priority (1–5), approval level (1–3), title, or description. "
        "Requires a task ID."
    )
    trigger_intents   = ["task_update"]
    approval_category = ApprovalCategory.STANDARD

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        task_id = params.get("id") or params.get("task_id")
        if not task_id:
            return SkillResult(
                context_data=(
                    "[task_update requires a task ID. "
                    "Ask the user which task they want to update.]"
                ),
                skill_name=self.name,
            )

        from app.db import postgres

        row = postgres.execute_one(
            """
            SELECT id, title, status, priority_num, approval_level
            FROM   tasks WHERE id = %s
            """,
            (int(task_id),),
        )
        if not row:
            return SkillResult(
                context_data=f"[No task found with ID #{task_id}]",
                skill_name=self.name,
            )

        changes: list[str] = []

        if params.get("status"):
            changes.append(f"Status: {row['status']} → **{params['status']}**")

        if params.get("priority") is not None:
            pri = max(1, min(5, int(params["priority"])))
            old = _PRIORITY_LABEL.get(row.get("priority_num") or 3, "?")
            changes.append(f"Priority: {old} → **{_PRIORITY_LABEL.get(pri)}**")

        if params.get("approval_level") is not None:
            alv = max(1, min(3, int(params["approval_level"])))
            old = _APPROVAL_LABEL.get(row.get("approval_level") or 2, "?")
            changes.append(f"Approval level: {old} → **{_APPROVAL_LABEL.get(alv)}**")

        if params.get("title"):
            changes.append(f"Title: _{row['title']}_ → **{params['title']}**")

        if params.get("description"):
            changes.append("Description updated")

        if not changes:
            return SkillResult(
                context_data=(
                    f"[No changes specified for task #{task_id}. "
                    "Please provide status, priority, approval_level, or title to update.]"
                ),
                skill_name=self.name,
            )

        context = (
            f"Confirm update to task **#{row['id']} — {row['title']}**:\n\n"
            + "\n".join(f"• {c}" for c in changes)
            + "\n\nReply **confirm** to apply or **cancel** to abort."
        )

        return SkillResult(
            context_data=context,
            pending_action={
                "action":   "task_update",
                "params":   params,
                "original": original_message,
            },
            skill_name=self.name,
        )
