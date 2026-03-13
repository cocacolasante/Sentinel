"""
Task Skills

TaskCreateSkill — create a task with priority (1–5) and approval level (1–3)
TaskReadSkill   — list, search, and view tasks
TaskUpdateSkill — update a task's status, priority, or approval level
"""

from __future__ import annotations

import logging

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)

_PRIORITY_LABEL = {
    1: "🟢 Low (1)",
    2: "🔵 Minor (2)",
    3: "🟡 Normal (3)",
    4: "🟠 High (4)",
    5: "🔴 Critical (5)",
}
_APPROVAL_LABEL = {
    1: "auto-approve",
    2: "needs review",
    3: "requires sign-off",
}
_STATUS_EMOJI = {
    "pending": "⏳",
    "in_progress": "🔄",
    "done": "✅",
    "cancelled": "❌",
}
_PRIORITY_TO_TEXT = {1: "low", 2: "low", 3: "normal", 4: "high", 5: "urgent"}
_TEXT_TO_PRIORITY = {"low": 1, "normal": 3, "high": 4, "urgent": 5}


def _fmt_task(row: dict) -> str:
    pri = row.get("priority_num") or _TEXT_TO_PRIORITY.get(row.get("priority", "normal"), 3)
    app = row.get("approval_level", 2)
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
    name = "task_create"
    description = (
        "Create a new task on the task board with title, description, priority, and due date. "
        "Use when Anthony says 'create task', 'add to my task list', 'make a task for', "
        "'put this on my board', or 'track this as a task'. "
        "NOT for: reading existing tasks (use task_read) or updating tasks (use task_update)."
    )
    trigger_intents = ["task_create"]
    approval_category = ApprovalCategory.NONE  # DB insert — no external side-effects

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        import json as _json
        from app.db import postgres

        # ── Multi-task batch: {"tasks": [...]} ────────────────────────────────
        task_list = params.get("tasks")
        if isinstance(task_list, list) and task_list:
            results = []
            for task_params in task_list:
                if not isinstance(task_params, dict):
                    continue
                merged = {**params, **task_params}
                merged.pop("tasks", None)
                result = await self.execute(merged, original_message)
                results.append(result.context_data or "")
            combined = "\n\n---\n\n".join(results)
            return SkillResult(context_data=combined, skill_name=self.name)

        title = (params.get("title") or "").strip()
        description = params.get("description", "")
        priority_num = max(1, min(5, int(params.get("priority", 3))))
        # Default to auto-approve (1) so scan_pending_tasks picks the task up within 1 min.
        # Use 2 (needs review) or 3 (requires sign-off) only when the caller explicitly requests it.
        approval_level = max(1, min(3, int(params.get("approval_level", 1))))
        due_date = params.get("due_date") or None
        source = params.get("source", "brain")
        tags = params.get("tags") or None
        assigned_to = params.get("assigned_to") or None
        session_id = (params.get("session_id") or "").strip()

        # blocked_by: list of task IDs that must be done before this task runs
        raw_blocked = params.get("blocked_by") or []
        if isinstance(raw_blocked, str):
            try:
                raw_blocked = _json.loads(raw_blocked)
            except Exception as e:
                logger.warning("task_create: could not parse blocked_by JSON %r: %s", raw_blocked, e)
                raw_blocked = []
        blocked_by: list[int] = [int(x) for x in raw_blocked if x]

        # Background execution fields
        raw_commands = params.get("commands") or []
        if isinstance(raw_commands, str):
            try:
                raw_commands = _json.loads(raw_commands)
            except Exception as e:
                logger.warning("task_create: could not parse commands JSON %r: %s", raw_commands, e)
                raw_commands = [raw_commands]
        commands: list[str] = [c for c in raw_commands if c and c.strip()]

        # Auto-detect queue: workspace tasks serialised, everything else parallel
        from app.worker.tasks import _touches_workspace

        uses_workspace = _touches_workspace(commands)
        execution_queue = "tasks_workspace" if uses_workspace else "tasks_general"
        # Allow explicit override
        if params.get("execution_queue"):
            execution_queue = params["execution_queue"]

        if not title:
            return SkillResult(
                context_data=("[task_create requires a task title. Ask the user: what should the task be called?]"),
                skill_name=self.name,
            )

        priority_text = _PRIORITY_TO_TEXT[priority_num]
        commands_json = _json.dumps(commands)
        blocked_by_json = _json.dumps(blocked_by)

        # Look up stored Slack context so the task can report back
        slack_channel = slack_thread_ts = None
        if session_id:
            try:
                from app.memory.redis_client import RedisMemory

                ctx = RedisMemory().get_slack_context(session_id)
                if ctx:
                    slack_channel = ctx.get("channel")
                    slack_thread_ts = ctx.get("thread_ts")
            except Exception as e:
                logger.warning("task_create: could not fetch Slack context for session %s: %s", session_id, e)

        try:
            row = postgres.execute_one(
                """
                INSERT INTO tasks
                    (title, description, status, priority, priority_num, approval_level,
                     due_date, source, tags, assigned_to,
                     commands, execution_queue, slack_channel, slack_thread_ts, session_id,
                     blocked_by)
                VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s,
                        %s::jsonb)
                RETURNING id, title, status, priority, priority_num, approval_level,
                          due_date, source, tags, assigned_to, created_at, blocked_by
                """,
                (
                    title,
                    description,
                    priority_text,
                    priority_num,
                    approval_level,
                    due_date,
                    source,
                    tags,
                    assigned_to,
                    commands_json,
                    execution_queue,
                    slack_channel,
                    slack_thread_ts,
                    session_id or None,
                    blocked_by_json,
                ),
            )
        except Exception as exc:
            return SkillResult(context_data=f"[task_create failed: {exc}]", skill_name=self.name)

        # Notify sentinel-tasks channel (fire-and-forget, errors are non-fatal)
        try:
            from app.integrations.task_notifier import post_task_created
            import asyncio as _asyncio
            _asyncio.create_task(
                post_task_created(
                    row["id"], title, priority_num, approval_level,
                    description or "", source,
                )
            )
        except Exception as e:
            logger.warning("task_create: could not post task-created notification for task #%s: %s", row["id"], e)

        queue_note = ""
        celery_id = None

        # Auto-queue if commands were provided
        if commands:
            # Tasks with approval_level >= 2 require owner sign-off before running
            if approval_level >= 2:
                from app.config import get_settings as _gs

                _s = _gs()
                if _s.slack_owner_user_id and _s.slack_bot_token:
                    from app.integrations.slack_notifier import post_dm_sync, post_alert_sync

                    _domain = _s.domain or "sentinelai.cloud"
                    _dm_text = (
                        f"🔐 *Approval needed — Task #{row['id']}*\n"
                        f"Action: {title}\n"
                        f"Category: {'requires sign-off' if approval_level == 3 else 'needs review'}\n\n"
                        f"✅ Approve: `POST https://{_domain}/api/v1/board/tasks/{row['id']}` "
                        f"(set status=in_progress)\n"
                        f"❌ Cancel: `DELETE https://{_domain}/api/v1/board/tasks/{row['id']}`\n\n"
                        "Or reply *confirm* / *cancel* in the originating Slack thread."
                    )
                    post_dm_sync(_dm_text)
                    post_alert_sync(
                        f"🔐 *Approval needed — Task #{row['id']}: {title}*\n"
                        f"Approval level: {_APPROVAL_LABEL.get(approval_level)}\n"
                        f"DM sent to owner for review."
                    )
                queue_note = (
                    f"\n⏳ Task #{row['id']} requires approval (level {approval_level}) before it runs. "
                    "I've DM'd the owner for sign-off."
                )
            else:
                try:
                    from app.worker.tasks import execute_board_task

                    result = execute_board_task.apply_async(
                        args=[row["id"]],
                        queue=execution_queue,
                    )
                    celery_id = result.id
                    postgres.execute(
                        "UPDATE tasks SET celery_task_id=%s WHERE id=%s",
                        (celery_id, row["id"]),
                    )
                    lock_note = " (serialised — workspace lock acquired when it starts)" if uses_workspace else ""
                    queue_note = (
                        f"\n🔄 Queued on `{execution_queue}`{lock_note} — "
                        f"Celery ID `{celery_id[:8]}…`  "
                        "I'll post the result back to this thread when done."
                    )
                except Exception as exc:
                    queue_note = f"\n⚠️ Task created but could not queue: {exc}"

        context = (
            "Task created successfully!\n\n"
            + _fmt_task(row)
            + f"\n\nTask ID: #{row['id']} | Created: {str(row['created_at'])[:19]}"
            + queue_note
        )
        return SkillResult(context_data=context, skill_name=self.name)


class TaskReadSkill(BaseSkill):
    name = "task_read"
    description = (
        "Read tasks from the task board: list all tasks, filter by status/priority/tag, or get a specific task. "
        "Use when Anthony says 'show my tasks', 'what tasks do I have', 'list pending tasks', "
        "'what's on my board', or 'show task #[ID]'. "
        "NOT for: creating tasks (use task_create) or updating them (use task_update)."
    )
    trigger_intents = ["task_read"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.db import postgres

        action = params.get("action", "list")
        task_id = params.get("id") or params.get("task_id")
        status = params.get("status")
        priority = params.get("priority")
        limit = max(1, min(100, int(params.get("limit", 20))))

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

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
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
    name = "task_update"
    description = (
        "Update an existing task: change status (pending/in_progress/completed), update description, "
        "set priority, or add notes. "
        "Use when Anthony says 'mark task done', 'update task [ID]', 'complete this task', "
        "'move to in-progress', or 'close task #[N]'. "
        "NOT for: creating new tasks (use task_create) or reading tasks (use task_read)."
    )
    trigger_intents = ["task_update"]
    approval_category = ApprovalCategory.STANDARD

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        task_id = params.get("id") or params.get("task_id")
        if not task_id:
            return SkillResult(
                context_data=("[task_update requires a task ID. Ask the user which task they want to update.]"),
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
                "action": "task_update",
                "params": params,
                "original": original_message,
            },
            skill_name=self.name,
        )
