"""
Task Board Router — REST API for the task management skill.

Endpoints:
  GET    /api/v1/board/tasks           — list tasks (with optional filters)
  POST   /api/v1/board/tasks           — create a task
  GET    /api/v1/board/tasks/{id}      — get a single task
  PATCH  /api/v1/board/tasks/{id}      — update a task
  DELETE /api/v1/board/tasks/{id}      — cancel a task (soft-delete)
"""

from __future__ import annotations

import json as _json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import postgres

router = APIRouter()

_PRIORITY_TO_TEXT = {1: "low", 2: "low", 3: "normal", 4: "high", 5: "urgent"}
_PRIORITY_LABEL   = {1: "Low", 2: "Minor", 3: "Normal", 4: "High", 5: "Critical"}
_APPROVAL_LABEL   = {1: "auto-approve", 2: "needs review", 3: "requires sign-off"}


# ── Request / Response models ──────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title:          str
    description:    str        = ""
    priority:       int        = 3   # 1–5
    approval_level: int        = 2   # 1–3
    due_date:       str | None = None
    source:         str        = "brain"
    tags:           str        = ""
    assigned_to:    str        = ""
    blocked_by:     list[int]  = []  # task IDs that must be done first


class TaskUpdate(BaseModel):
    title:          str | None       = None
    description:    str | None       = None
    status:         str | None       = None   # pending | in_progress | done | cancelled
    priority:       int | None       = None
    approval_level: int | None       = None
    due_date:       str | None       = None
    tags:           str | None       = None
    assigned_to:    str | None       = None
    blocked_by:     list[int] | None = None  # replace the blocked_by list


# ── Helpers ────────────────────────────────────────────────────────────────────

def _enrich(row: dict) -> dict:
    """Add human-readable labels to a raw task row."""
    row = dict(row)
    pri = row.get("priority_num") or 3
    alv = row.get("approval_level") or 2
    row["priority_label"]   = _PRIORITY_LABEL.get(pri, str(pri))
    row["approval_label"]   = _APPROVAL_LABEL.get(alv, str(alv))
    return row


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/board/tasks")
async def list_tasks(
    status:   str | None = Query(None, description="Filter by status"),
    priority: int | None = Query(None, ge=1, le=5, description="Filter by priority (1–5)"),
    limit:    int        = Query(50,   ge=1, le=200),
):
    conditions: list[str] = []
    values: list[Any] = []

    if status:
        conditions.append("status = %s")
        values.append(status)
    if priority is not None:
        conditions.append("priority_num = %s")
        values.append(priority)

    where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    values.append(limit)

    rows = postgres.execute(
        f"""
        SELECT id, title, description, status, priority, priority_num,
               approval_level, due_date, source, tags, assigned_to,
               blocked_by, created_at, updated_at
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
    return {"tasks": [_enrich(r) for r in rows], "count": len(rows)}


@router.post("/board/tasks", status_code=201)
async def create_task(body: TaskCreate):
    priority_num   = max(1, min(5, body.priority))
    approval_level = max(1, min(3, body.approval_level))
    priority_text  = _PRIORITY_TO_TEXT[priority_num]

    row = postgres.execute_one(
        """
        INSERT INTO tasks
            (title, description, status, priority, priority_num, approval_level,
             due_date, source, tags, assigned_to, blocked_by)
        VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING id, title, description, status, priority, priority_num,
                  approval_level, due_date, source, tags, assigned_to,
                  blocked_by, created_at, updated_at
        """,
        (
            body.title, body.description, priority_text, priority_num, approval_level,
            body.due_date or None, body.source, body.tags or None, body.assigned_to or None,
            _json.dumps(body.blocked_by),
        ),
    )
    return _enrich(row)


@router.get("/board/tasks/{task_id}")
async def get_task(task_id: int):
    row = postgres.execute_one(
        """
        SELECT id, title, description, status, priority, priority_num,
               approval_level, due_date, source, tags, assigned_to,
               blocked_by, created_at, updated_at
        FROM   tasks WHERE id = %s
        """,
        (task_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Task #{task_id} not found")
    return _enrich(row)


@router.patch("/board/tasks/{task_id}")
async def update_task(task_id: int, body: TaskUpdate):
    existing = postgres.execute_one(
        "SELECT id FROM tasks WHERE id = %s", (task_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail=f"Task #{task_id} not found")

    fields: list[str] = []
    values: list[Any] = []

    if body.title is not None:
        fields.append("title = %s");       values.append(body.title)
    if body.description is not None:
        fields.append("description = %s"); values.append(body.description)
    if body.status is not None:
        fields.append("status = %s");      values.append(body.status)
        # Clear celery_task_id when resetting to pending so scan can re-dispatch
        if body.status == "pending":
            fields.append("celery_task_id = NULL")
    if body.priority is not None:
        pri = max(1, min(5, body.priority))
        fields.append("priority_num = %s"); values.append(pri)
        fields.append("priority = %s");     values.append(_PRIORITY_TO_TEXT[pri])
    if body.approval_level is not None:
        alv = max(1, min(3, body.approval_level))
        fields.append("approval_level = %s"); values.append(alv)
    if body.due_date is not None:
        fields.append("due_date = %s");    values.append(body.due_date or None)
    if body.tags is not None:
        fields.append("tags = %s");        values.append(body.tags or None)
    if body.assigned_to is not None:
        fields.append("assigned_to = %s"); values.append(body.assigned_to or None)
    if body.blocked_by is not None:
        fields.append("blocked_by = %s::jsonb"); values.append(_json.dumps(body.blocked_by))

    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    fields.append("updated_at = NOW()")
    values.append(task_id)

    row = postgres.execute_one(
        f"""
        UPDATE tasks SET {', '.join(fields)} WHERE id = %s
        RETURNING id, title, description, status, priority, priority_num,
                  approval_level, due_date, source, tags, assigned_to,
                  blocked_by, created_at, updated_at
        """,
        values,
    )
    return _enrich(row)


@router.delete("/board/tasks/{task_id}")
async def cancel_task(task_id: int):
    row = postgres.execute_one(
        """
        UPDATE tasks SET status = 'cancelled', updated_at = NOW()
        WHERE  id = %s
        RETURNING id, title, status
        """,
        (task_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Task #{task_id} not found")
    return {"message": f"Task #{task_id} cancelled", "task": row}


@router.delete("/board/tasks/{task_id}/purge")
async def purge_task(task_id: int):
    """Hard-delete a single task (no soft cancel — permanently removed)."""
    row = postgres.execute_one(
        "DELETE FROM tasks WHERE id = %s RETURNING id, title",
        (task_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Task #{task_id} not found")
    return {"deleted": True, "id": row["id"], "title": row["title"]}


class TaskPurge(BaseModel):
    statuses: list[str]  # e.g. ["pending", "in_progress", "cancelled"]


@router.post("/board/tasks/purge")
async def purge_tasks_bulk(body: TaskPurge):
    """Hard-delete all tasks matching the given statuses."""
    _VALID = {"pending", "in_progress", "cancelled", "done", "failed"}
    statuses = [s for s in body.statuses if s in _VALID]
    if not statuses:
        raise HTTPException(status_code=400, detail="No valid statuses provided")
    placeholders = ", ".join(["%s"] * len(statuses))
    rows = postgres.execute(
        f"DELETE FROM tasks WHERE status IN ({placeholders}) RETURNING id",
        statuses,
    )
    return {"deleted": len(rows or []), "statuses": statuses}


@router.get("/board/activity")
async def get_activity():
    """Live AI activity feed: in-progress tasks, pending queue, recent milestones."""
    in_progress = postgres.execute(
        """
        SELECT id, title, description, status, assigned_to, priority_num, updated_at
        FROM   tasks
        WHERE  status = 'in_progress'
        ORDER  BY updated_at DESC
        """,
        [],
    ) or []

    pending_next = postgres.execute(
        """
        SELECT id, title, status, priority_num
        FROM   tasks
        WHERE  status = 'pending'
        ORDER  BY priority_num DESC, created_at ASC
        LIMIT  5
        """,
        [],
    ) or []

    recent_milestones = postgres.execute(
        """
        SELECT action, intent, summary, agent, triggered_at
        FROM   ai_milestones
        ORDER  BY triggered_at DESC
        LIMIT  8
        """,
        [],
    ) or []

    def _fmt(row: dict) -> dict:
        r = dict(row)
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
        return r

    return {
        "in_progress":        [_fmt(r) for r in in_progress],
        "pending_next":       [_fmt(r) for r in pending_next],
        "recent_milestones":  [_fmt(r) for r in recent_milestones],
    }
