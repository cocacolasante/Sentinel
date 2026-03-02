"""
Cost and rate limit status endpoints.

GET /api/v1/costs — live daily spend breakdown and remaining budget.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/costs", summary="Daily LLM cost summary")
async def get_costs() -> dict:
    """
    Return today's LLM spend broken down by model, with ceiling and remaining budget.

    Example response:
    {
      "date": "2026-03-01",
      "total_cost_usd": 0.142300,
      "daily_ceiling_usd": 10.0,
      "pct_of_ceiling": 1.4,
      "remaining_usd": 9.857700,
      "rate_limits": {"per_minute": 20, "per_hour": 200},
      "models": {
        "claude-sonnet-4-6": {
          "tokens_in": 42000, "tokens_out": 6800, "tokens_total": 48800,
          "cost_usd": 0.228000, "token_budget": null, "pct_of_budget": null
        }
      }
    }
    """
    from app.brain.cost_tracker import cost_tracker
    return cost_tracker.get_daily_summary()
