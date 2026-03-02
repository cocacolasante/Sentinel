"""
REST Chat Endpoint — /api/v1/chat

All messages go through the Dispatcher which handles intent classification,
integration calls, LLM augmentation, and Redis memory.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.brain.dispatcher  import Dispatcher
from app.brain.llm_router  import get_telos_loader
from app.memory.redis_client import RedisMemory

router   = APIRouter()
dispatch = Dispatcher()
memory   = RedisMemory()


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message:    str = Field(..., min_length=1, max_length=8000)
    session_id: str = Field(default="default", max_length=128)


class ChatResponse(BaseModel):
    reply:      str
    session_id: str
    intent:     str
    agent:      str = "default"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    try:
        result = await dispatch.process(req.message, req.session_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(
        reply=result.reply,
        session_id=result.session_id,
        intent=result.intent,
        agent=result.agent,
    )


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


@router.get("/health")
async def health():
    from app.db import postgres
    return {
        "status":   "ok",
        "redis":    memory.ping(),
        "postgres": postgres.ping(),
    }
