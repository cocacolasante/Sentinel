"""
GitHub Monitor REST endpoints.

GET  /api/v1/github/monitors           → list all monitors
POST /api/v1/github/monitors           → add monitor
PATCH /api/v1/github/monitors/{id}     → update monitor
DELETE /api/v1/github/monitors/{id}    → remove monitor
GET  /api/v1/github/issues             → list tracked issues
POST /api/v1/github/poll               → manually trigger a poll run
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class MonitorCreate(BaseModel):
    repo: str                                    # "owner/repo"
    label: Optional[str] = None
    agent_id: Optional[str] = None
    issue_filter: str = "is:open is:issue"
    poll_labels: Optional[str] = None
    enabled: bool = True


class MonitorUpdate(BaseModel):
    enabled: Optional[bool] = None
    agent_id: Optional[str] = None
    issue_filter: Optional[str] = None
    poll_labels: Optional[str] = None
    label: Optional[str] = None


# ── Monitors ──────────────────────────────────────────────────────────────────


@router.get("/github/monitors")
async def list_monitors():
    """List all GitHub repo monitors with their current state."""
    from app.db import postgres

    rows = postgres.execute(
        """
        SELECT g.id, g.repo, g.label, g.enabled, g.agent_id,
               g.issue_filter, g.poll_labels, g.last_polled_at, g.created_at,
               m.app_name AS agent_name, m.hostname AS agent_hostname
        FROM github_repo_monitors g
        LEFT JOIN mesh_agents m ON g.agent_id = m.agent_id
        ORDER BY g.repo
        """
    )
    return {"monitors": [dict(r) for r in (rows or [])]}


@router.post("/github/monitors", status_code=201)
async def add_monitor(body: MonitorCreate):
    """Add a new GitHub repo monitor."""
    from app.db import postgres

    try:
        row = postgres.execute_one(
            """
            INSERT INTO github_repo_monitors
                (repo, label, enabled, agent_id, issue_filter, poll_labels)
            VALUES (%s, %s, %s, %s::uuid, %s, %s)
            ON CONFLICT (repo) DO UPDATE SET
                label = EXCLUDED.label,
                enabled = EXCLUDED.enabled,
                agent_id = EXCLUDED.agent_id,
                issue_filter = EXCLUDED.issue_filter,
                poll_labels = EXCLUDED.poll_labels,
                updated_at = NOW()
            RETURNING id, repo, enabled
            """,
            (
                body.repo,
                body.label,
                body.enabled,
                body.agent_id or None,
                body.issue_filter,
                body.poll_labels,
            ),
        )
    except Exception as exc:
        logger.error("Failed to insert github_repo_monitors: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {"monitor": dict(row)}


@router.patch("/github/monitors/{monitor_id}")
async def update_monitor(monitor_id: int, body: MonitorUpdate):
    """Update an existing monitor (enabled flag, agent assignment, filter)."""
    from app.db import postgres

    existing = postgres.execute_one(
        "SELECT id FROM github_repo_monitors WHERE id = %s", (monitor_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Monitor not found")

    updates: list[str] = ["updated_at = NOW()"]
    params: list = []

    if body.enabled is not None:
        updates.append("enabled = %s")
        params.append(body.enabled)
    if body.agent_id is not None:
        updates.append("agent_id = %s::uuid")
        params.append(body.agent_id if body.agent_id else None)
    if body.issue_filter is not None:
        updates.append("issue_filter = %s")
        params.append(body.issue_filter)
    if body.poll_labels is not None:
        updates.append("poll_labels = %s")
        params.append(body.poll_labels)
    if body.label is not None:
        updates.append("label = %s")
        params.append(body.label)

    if len(updates) == 1:
        return {"updated": False, "message": "No fields to update"}

    params.append(monitor_id)
    postgres.execute(
        f"UPDATE github_repo_monitors SET {', '.join(updates)} WHERE id = %s",
        params,
    )
    return {"updated": True, "id": monitor_id}


@router.delete("/github/monitors/{monitor_id}", status_code=204)
async def delete_monitor(monitor_id: int):
    """Remove a GitHub repo monitor."""
    from app.db import postgres

    existing = postgres.execute_one(
        "SELECT id FROM github_repo_monitors WHERE id = %s", (monitor_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Monitor not found")

    postgres.execute("DELETE FROM github_repo_monitors WHERE id = %s", (monitor_id,))


# ── Issues ────────────────────────────────────────────────────────────────────


@router.get("/github/issues")
async def list_issues(repo: Optional[str] = None, status: Optional[str] = None, limit: int = 50):
    """List tracked GitHub issues with optional filtering."""
    from app.db import postgres

    where_clauses = []
    params: list = []
    if repo:
        where_clauses.append("repo = %s")
        params.append(repo)
    if status:
        where_clauses.append("triage_status = %s")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(min(limit, 200))

    rows = postgres.execute(
        f"""
        SELECT id, repo, issue_number, title, state, labels, triage_status,
               pr_url, task_id, received_at, updated_at
        FROM github_issues
        {where_sql}
        ORDER BY received_at DESC
        LIMIT %s
        """,
        params or None,
    )
    return {"issues": [dict(r) for r in (rows or [])], "count": len(rows or [])}


# ── Manual poll trigger ───────────────────────────────────────────────────────


@router.post("/github/poll")
async def trigger_poll(repo: Optional[str] = None):
    """Manually trigger a GitHub issue poll run."""
    from app.worker.github_tasks import ingest_and_triage_github_issues

    task = ingest_and_triage_github_issues.apply_async(
        kwargs={"repo": repo} if repo else {},
        queue="celery",
    )
    return {"queued": True, "celery_task_id": task.id, "repo": repo or "all"}
