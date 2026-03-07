"""
REST Chat Endpoint — /api/v1/chat

All messages go through the Dispatcher which handles intent classification,
integration calls, LLM augmentation, and Redis memory.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.brain.dispatcher import Dispatcher
from app.brain.llm_router import get_telos_loader
from app.config import get_settings
from app.memory.redis_client import RedisMemory

router = APIRouter()
dispatch = Dispatcher()
memory = RedisMemory()
settings = get_settings()


# ── Schemas ───────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    # If omitted or "default", the primary shared session is used so that
    # REST API requests participate in cross-interface memory.
    session_id: str = Field(default="", max_length=128)


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    intent: str
    agent: str = "default"


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    # Map empty / "default" to the primary shared session so every interface
    # contributes to and reads from the same warm-memory pool.
    sid = req.session_id.strip()
    if not sid or sid == "default":
        sid = settings.brain_primary_session

    try:
        result = await dispatch.process(req.message, sid)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(
        reply=result.reply,
        session_id=result.session_id,
        intent=result.intent,
        agent=result.agent,
    )


@router.get("/chat/followup/{session_id}")
async def poll_followup(session_id: str):
    """
    Long-poll endpoint for the Grafana chat panel.
    Returns the next queued follow-up message (and removes it), or null if none.
    The panel calls this repeatedly after a send to catch async replies.
    """
    msg = memory.pop_followup(session_id)
    return {"message": msg}


@router.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    """Clear conversation history and any pending actions for a session."""
    memory.clear_session(session_id)
    memory.clear_pending_action(session_id)
    return {"cleared": session_id}


@router.post("/telos/reload")
async def telos_reload():
    """Force reload of TELOS personal context files from disk."""
    loader = get_telos_loader()
    files = loader.reload()
    return {"reloaded": files}


@router.get("/agents")
async def list_agents():
    """List all registered agent personalities."""
    from app.agents.registry import AgentRegistry

    registry = AgentRegistry()
    return {"agents": registry.list_agents()}


_STARTED_AT: str = ""


def _get_started_at() -> str:
    global _STARTED_AT
    if not _STARTED_AT:
        from datetime import datetime, timezone
        _STARTED_AT = datetime.now(timezone.utc).isoformat()
    return _STARTED_AT


@router.get("/health")
async def health():
    import os
    from app.db import postgres

    return {
        "status": "ok",
        "redis": memory.ping(),
        "postgres": postgres.ping(),
        "sha": os.environ.get("GIT_SHA", "dev"),
        "started_at": _get_started_at(),
    }


@router.get("/version")
async def version():
    import os
    from datetime import datetime, timezone

    return {
        "app": "sentinel-brain",
        "version": "2.1.0",
        "sha": os.environ.get("GIT_SHA", "dev"),
        "environment": settings.environment,
        "built_at": os.environ.get("BUILD_TIMESTAMP", "unknown"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
