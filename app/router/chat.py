"""
REST Chat Endpoint — /api/v1/chat

All messages go through the Dispatcher which handles intent classification,
integration calls, LLM augmentation, and Redis memory.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.brain.dispatcher  import Dispatcher
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
    )


@router.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    """Clear conversation history and any pending actions for a session."""
    memory.clear_session(session_id)
    memory.clear_pending_action(session_id)
    return {"cleared": session_id}


@router.get("/health")
async def health():
    from app.db import postgres
    return {
        "status":   "ok",
        "redis":    memory.ping(),
        "postgres": postgres.ping(),
    }
