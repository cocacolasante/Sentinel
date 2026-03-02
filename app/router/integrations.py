"""
Integration Management Endpoints — /api/v1/integrations

Provides:
  GET  /integrations/status           — health check for all integrations
  GET  /integrations/gmail            — list unread emails (direct)
  GET  /integrations/calendar         — list upcoming events (direct)
  GET  /integrations/github           — list notifications (direct)
  GET  /integrations/home-assistant   — list HA entity states (direct)
  POST /integrations/n8n/trigger      — manually trigger an n8n workflow
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.integrations.gmail           import GmailClient
from app.integrations.google_calendar import CalendarClient
from app.integrations.github          import GitHubClient
from app.integrations.n8n_bridge      import N8nBridge
from app.integrations.home_assistant  import HomeAssistantClient

router = APIRouter(prefix="/integrations", tags=["integrations"])
logger = logging.getLogger(__name__)

# Singletons
gmail    = GmailClient()
calendar = CalendarClient()
github   = GitHubClient()
n8n      = N8nBridge()
ha       = HomeAssistantClient()


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def integration_status() -> dict:
    """Return configuration status for all Phase 2 integrations."""
    n8n_ok = await n8n.health()
    ha_ok  = await ha.health() if ha.is_configured() else False
    return {
        "gmail":          gmail.is_configured(),
        "calendar":       calendar.is_configured(),
        "github":         github.is_configured(),
        "n8n":            {"configured": n8n.is_configured(), "reachable": n8n_ok},
        "home_assistant": {"configured": ha.is_configured(),  "reachable": ha_ok},
    }


# ── Gmail ─────────────────────────────────────────────────────────────────────

@router.get("/gmail")
async def list_emails(
    query:       str = Query(default="is:unread", description="Gmail search query"),
    max_results: int = Query(default=10, ge=1, le=50),
) -> list[dict]:
    if not gmail.is_configured():
        raise HTTPException(status_code=503, detail="Gmail not configured")
    return await gmail.list_emails(query=query, max_results=max_results)


# ── Calendar ──────────────────────────────────────────────────────────────────

@router.get("/calendar")
async def list_events(
    period: str = Query(default="this week", description="today / tomorrow / this week / next week"),
) -> list[dict]:
    if not calendar.is_configured():
        raise HTTPException(status_code=503, detail="Google Calendar not configured")
    return await calendar.list_events(period=period)


@router.get("/calendar/free-slots")
async def free_slots(
    date:         str = Query(..., description="Date in YYYY-MM-DD format"),
    duration_min: int = Query(default=60, ge=15, le=480),
) -> list[dict]:
    if not calendar.is_configured():
        raise HTTPException(status_code=503, detail="Google Calendar not configured")
    return await calendar.find_free_slots(date=date, duration_min=duration_min)


# ── GitHub ────────────────────────────────────────────────────────────────────

@router.get("/github/notifications")
async def github_notifications() -> list[dict]:
    if not github.is_configured():
        raise HTTPException(status_code=503, detail="GitHub not configured")
    return await github.list_notifications()


@router.get("/github/issues")
async def github_issues(
    repo:  str = Query(default="", description="owner/repo — defaults to GITHUB_DEFAULT_REPO"),
    state: str = Query(default="open"),
) -> list[dict]:
    if not github.is_configured():
        raise HTTPException(status_code=503, detail="GitHub not configured")
    return await github.list_issues(repo=repo, state=state)


@router.get("/github/prs")
async def github_prs(
    repo:  str = Query(default=""),
    state: str = Query(default="open"),
) -> list[dict]:
    if not github.is_configured():
        raise HTTPException(status_code=503, detail="GitHub not configured")
    return await github.list_prs(repo=repo, state=state)


# ── Home Assistant ────────────────────────────────────────────────────────────

@router.get("/home-assistant/states")
async def ha_states(
    domain: str = Query(default="", description="Filter by domain e.g. 'light', 'switch'"),
) -> list[dict]:
    if not ha.is_configured():
        raise HTTPException(status_code=503, detail="Home Assistant not configured")
    return await ha.list_entities(domain=domain or None)


@router.get("/home-assistant/entity/{entity_id:path}")
async def ha_entity(entity_id: str) -> dict:
    if not ha.is_configured():
        raise HTTPException(status_code=503, detail="Home Assistant not configured")
    return await ha.get_entity(entity_id)


class ServiceCall(BaseModel):
    domain:  str
    service: str
    data:    dict = {}


@router.post("/home-assistant/service")
async def ha_call_service(call: ServiceCall) -> dict:
    if not ha.is_configured():
        raise HTTPException(status_code=503, detail="Home Assistant not configured")
    return await ha.call_service(call.domain, call.service, call.data)


# ── n8n ───────────────────────────────────────────────────────────────────────

class N8nTrigger(BaseModel):
    workflow: str
    payload:  dict = {}


@router.post("/n8n/trigger")
async def trigger_n8n(req: N8nTrigger) -> dict:
    if not n8n.is_configured():
        raise HTTPException(status_code=503, detail="n8n not configured")
    return await n8n.trigger(req.workflow, req.payload)
