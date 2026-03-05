"""
GET /api/v1/milestones  — recent AI action milestones for the Grafana dashboard.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/milestones")
def list_milestones(limit: int = Query(default=20, ge=1, le=100)):
    from app.db import postgres

    rows = (
        postgres.execute(
            """
        SELECT id, session_id, action, intent, summary, agent, triggered_at
        FROM   ai_milestones
        ORDER  BY triggered_at DESC
        LIMIT  %s
        """,
            (limit,),
        )
        or []
    )

    return {
        "milestones": [
            {
                "id": r["id"],
                "session_id": r.get("session_id", ""),
                "action": r.get("action", ""),
                "intent": r.get("intent", ""),
                "summary": r.get("summary", ""),
                "agent": r.get("agent", ""),
                "triggered_at": r["triggered_at"].isoformat() if r.get("triggered_at") else None,
            }
            for r in rows
        ]
    }
