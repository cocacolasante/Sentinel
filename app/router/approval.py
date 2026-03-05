"""
Approval Level Router — /api/v1/approval/

Endpoints:
  GET  /approval/level          — current global approval level + label
  POST /approval/level          — set global approval level {level: 1|2|3}
  GET  /approval/pending        — write tasks awaiting approval (last 50)
  GET  /approval/history        — completed/failed write tasks (last 50)
  POST /approval/approve/{task_id}  — approve a waiting task (executes it)
  POST /approval/cancel/{task_id}   — cancel a waiting task
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import postgres
from app.memory.redis_client import RedisMemory

router = APIRouter(tags=["approval"])
_redis = RedisMemory()

_LEVEL_LABELS = {
    1: "All write operations require approval",
    2: "Only critical writes require approval — standard writes execute directly",
    3: "Only breaking/destructive changes require approval",
}


# ── Schemas ───────────────────────────────────────────────────────────────────


class SetLevelRequest(BaseModel):
    level: int


# ── Helpers ───────────────────────────────────────────────────────────────────


def _update_task_status(task_id: str, status: str, error: str | None = None) -> None:
    postgres.execute(
        """
        UPDATE pending_write_tasks
           SET status = %s, error = %s, updated_at = NOW()
         WHERE task_id = %s
        """,
        (status, error, task_id),
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/approval/level")
async def get_level():
    level = _redis.get_approval_level()
    return {
        "level": level,
        "label": _LEVEL_LABELS.get(level, "Unknown"),
    }


@router.post("/approval/level")
async def set_level(req: SetLevelRequest):
    if req.level not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="level must be 1, 2, or 3")
    _redis.set_approval_level(req.level)
    # Persist to Postgres so it survives Redis restart
    postgres.execute(
        """
        INSERT INTO brain_settings (key, value, updated_at)
        VALUES ('approval_level', %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (str(req.level),),
    )
    return {"level": req.level, "label": _LEVEL_LABELS[req.level]}


@router.get("/approval/pending")
async def list_pending():
    rows = postgres.execute(
        """
        SELECT task_id, session_id, action, title, category, status, created_at, updated_at
          FROM pending_write_tasks
         WHERE status = 'awaiting_approval'
         ORDER BY created_at DESC
         LIMIT 50
        """
    )
    return {"tasks": [dict(r) for r in rows]}


@router.get("/approval/history")
async def list_history():
    rows = postgres.execute(
        """
        SELECT task_id, session_id, action, title, category, status, error,
               created_at, updated_at
          FROM pending_write_tasks
         WHERE status IN ('completed', 'cancelled', 'failed')
         ORDER BY updated_at DESC
         LIMIT 50
        """
    )
    return {"tasks": [dict(r) for r in rows]}


@router.post("/approval/approve/{task_id}")
async def approve_task(task_id: str):
    """Approve and immediately execute a pending write task."""
    row = postgres.execute_one(
        "SELECT * FROM pending_write_tasks WHERE task_id = %s AND status = 'awaiting_approval'",
        (task_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found or not awaiting approval")

    _update_task_status(task_id, "executing")

    # Re-use _execute_pending from Dispatcher
    try:
        from app.brain.dispatcher import Dispatcher

        dispatcher = Dispatcher()
        pending_action = {
            "action": row["action"],
            "params": row["params"] or {},
            "intent": row["action"],
            "original": row["title"] or "",
        }
        reply = await dispatcher._execute_pending(pending_action, row["session_id"])
        _update_task_status(task_id, "completed")
        return {"task_id": task_id, "status": "completed", "reply": reply}
    except Exception as exc:
        _update_task_status(task_id, "failed", str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/approval/cancel/{task_id}")
async def cancel_task(task_id: str):
    row = postgres.execute_one(
        "SELECT task_id FROM pending_write_tasks WHERE task_id = %s AND status = 'awaiting_approval'",
        (task_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found or not awaiting approval")
    _update_task_status(task_id, "cancelled")
    return {"task_id": task_id, "status": "cancelled"}
