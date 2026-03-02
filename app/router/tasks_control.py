"""
Task & Worker Control — inspect, revoke, pause, and resume Celery tasks.

Endpoints:
  GET  /api/v1/tasks                  -- list active / reserved / scheduled
  DELETE /api/v1/tasks/{task_id}      -- revoke (optionally terminate) a task
  POST /api/v1/workers/pause          -- cancel queue consumers (stop processing)
  POST /api/v1/workers/resume         -- re-add queue consumers (resume processing)
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from app.worker.celery_app import celery_app

router = APIRouter(tags=["tasks"])

_QUEUES = ["celery", "evals"]
_INSPECT_TIMEOUT = 3.0


@router.get("/tasks")
async def list_tasks():
    """Return active, reserved, and scheduled tasks across all workers."""
    def _inspect():
        i = celery_app.control.inspect(timeout=_INSPECT_TIMEOUT)
        return {
            "active":    i.active()    or {},
            "reserved":  i.reserved()  or {},
            "scheduled": i.scheduled() or {},
        }

    try:
        return await asyncio.to_thread(_inspect)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Celery inspect failed: {exc}")


@router.delete("/tasks/{task_id}")
async def revoke_task(
    task_id: str,
    terminate: bool = Query(False, description="Send SIGTERM to the running process"),
):
    """Revoke a task. Set terminate=true to kill a running task immediately."""
    def _revoke():
        celery_app.control.revoke(task_id, terminate=terminate, signal="SIGTERM")

    await asyncio.to_thread(_revoke)
    return {"revoked": task_id, "terminated": terminate}


@router.post("/workers/pause")
async def pause_workers():
    """Cancel queue consumers on all workers — they finish the current task then stop."""
    def _pause():
        for queue in _QUEUES:
            celery_app.control.cancel_consumer(queue)

    await asyncio.to_thread(_pause)
    return {"status": "paused", "queues": _QUEUES}


@router.post("/workers/resume")
async def resume_workers():
    """Re-add queue consumers on all workers — they resume pulling tasks."""
    def _resume():
        for queue in _QUEUES:
            celery_app.control.add_consumer(queue)

    await asyncio.to_thread(_resume)
    return {"status": "resumed", "queues": _QUEUES}
