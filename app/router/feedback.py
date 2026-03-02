"""
Feedback Router — /api/v1/feedback

Captures user ratings on individual interactions for quality tracking and
Qdrant seeding of high-quality responses.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.learning.feedback_store import FeedbackStore

router   = APIRouter(prefix="/feedback", tags=["feedback"])
settings = get_settings()


def _store() -> FeedbackStore:
    return FeedbackStore(postgres_dsn=settings.postgres_dsn)


# ── Schemas ───────────────────────────────────────────────────────────────────

class RateRequest(BaseModel):
    session_id:    str          = Field(..., max_length=128)
    message_index: int          = Field(default=0, ge=0)
    rating:        int          = Field(..., ge=1, le=10)
    comment:       str | None   = Field(default=None, max_length=1000)
    intent:        str          = Field(default="chat", max_length=64)


class ThumbsRequest(BaseModel):
    session_id:    str  = Field(..., max_length=128)
    message_index: int  = Field(default=0, ge=0)
    positive:      bool


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/rate")
async def rate_interaction(req: RateRequest):
    """Store a 1-10 rating for a specific message in a session."""
    rating_id = _store().store_rating(
        session_id=req.session_id,
        message_index=req.message_index,
        rating=req.rating,
        comment=req.comment,
        intent=req.intent,
        source="api",
    )
    if rating_id < 0:
        raise HTTPException(status_code=500, detail="Failed to store rating")
    return {
        "id":            rating_id,
        "session_id":    req.session_id,
        "message_index": req.message_index,
        "rating":        req.rating,
        "qdrant_seeded": req.rating >= 8,
    }


@router.post("/thumbs")
async def thumbs(req: ThumbsRequest):
    """Convert a thumbs up/down to a rating (10 / 2) and store it."""
    mapped_rating = 10 if req.positive else 2
    rating_id = _store().store_rating(
        session_id=req.session_id,
        message_index=req.message_index,
        rating=mapped_rating,
        intent="chat",
        source="thumbs",
    )
    if rating_id < 0:
        raise HTTPException(status_code=500, detail="Failed to store rating")
    return {
        "id":      rating_id,
        "rating":  mapped_rating,
        "positive": req.positive,
    }


@router.get("/summary")
async def feedback_summary():
    """Return aggregate rating stats."""
    return _store().get_summary()
