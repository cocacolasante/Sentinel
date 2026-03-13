"""
CostTracker — per-model token budgets and daily cost ceiling enforcement.

Storage: Redis atomic counters (key prefix brain:cost:).
Keys never collide with hot memory (brain:session:*) or rate limiter (brain:rate:*).
All keys carry a 48-hour TTL so they expire automatically without a cleanup job.

Pricing is based on Anthropic's published per-million-token rates.
Update PRICING when Anthropic changes their pricing page.

Thresholds send a Slack DM once per crossing per day (SET NX guard).
Uses the synchronous slack_sdk.WebClient because CostTracker is called
from LLMRouter.route() which runs inside asyncio.to_thread().
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import redis as redis_lib
from loguru import logger

from app.config import get_settings

# ── Published Anthropic pricing (USD per 1M tokens, 2025) ─────────────────────
# Update these when Anthropic changes rates.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}
_FALLBACK_PRICING = {"input": 3.00, "output": 15.00}  # assume Sonnet rate for unknowns

_KEY_TTL = 172_800  # 48 hours — keys auto-expire without a cron job


# ── Exceptions ────────────────────────────────────────────────────────────────


class BudgetExceeded(Exception):
    """Raised before an LLM call when the daily ceiling or a model token budget is hit."""


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class CallCost:
    model: str
    input_tokens: int
    output_tokens: int
    call_cost_usd: float
    daily_total_usd: float
    daily_model_tokens: int


# ── Core class ────────────────────────────────────────────────────────────────


class CostTracker:
    """
    Thread-safe (uses Redis pipelines) cost tracker.

    Instantiate once at module level; share across all LLMRouter instances.
    """

    def __init__(self) -> None:
        self._redis: redis_lib.Redis | None = None

    # ── Redis connection (lazy) ───────────────────────────────────────────────

    @property
    def _r(self) -> redis_lib.Redis:
        if self._redis is None:
            s = get_settings()
            self._redis = redis_lib.Redis(
                host=s.redis_host,
                port=s.redis_port,
                password=s.redis_password,
                decode_responses=True,
            )
        return self._redis

    # ── Key helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _total_key(self, today: str) -> str:
        return f"brain:cost:daily:{today}:total"

    def _model_tin_key(self, today: str, model: str) -> str:
        return f"brain:cost:daily:{today}:model:{model}:tokens_in"

    def _model_tout_key(self, today: str, model: str) -> str:
        return f"brain:cost:daily:{today}:model:{model}:tokens_out"

    def _model_cost_key(self, today: str, model: str) -> str:
        return f"brain:cost:daily:{today}:model:{model}:cost"

    def _alert_key(self, today: str, pct: int) -> str:
        return f"brain:cost:alert:{today}:{pct}"

    # ── Cost calculation ──────────────────────────────────────────────────────

    @staticmethod
    def _calc_usd(model: str, input_tokens: int, output_tokens: int) -> float:
        p = PRICING.get(model, _FALLBACK_PRICING)
        return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000

    # ── Public API ────────────────────────────────────────────────────────────

    def check_budget(self, model: str) -> None:
        """
        Call BEFORE the Anthropic API call.

        Raises BudgetExceeded if:
        - Today's running total has already hit the daily ceiling, OR
        - This model's daily token count has hit its per-model budget.
        """
        s = get_settings()
        today = self._today()

        # ── Daily cost ceiling ─────────────────────────────────────────────
        if s.daily_cost_ceiling_usd > 0:
            current = float(self._r.get(self._total_key(today)) or 0)
            if current >= s.daily_cost_ceiling_usd:
                raise BudgetExceeded(
                    f"Daily cost ceiling reached: ${current:.4f} / ${s.daily_cost_ceiling_usd:.2f}. "
                    f"Resets at midnight UTC."
                )

        # ── Per-model token budget ─────────────────────────────────────────
        budget = self._model_token_budget(s, model)
        if budget:
            tin = int(self._r.get(self._model_tin_key(today, model)) or 0)
            tout = int(self._r.get(self._model_tout_key(today, model)) or 0)
            if tin + tout >= budget:
                raise BudgetExceeded(f"{model} token budget exhausted: {tin + tout:,} / {budget:,} tokens today.")

    def record(self, model: str, input_tokens: int, output_tokens: int) -> CallCost:
        """
        Call AFTER a successful Anthropic API call.

        Atomically increments all Redis counters in a pipeline and triggers
        threshold alerts if a new threshold has been crossed.
        """
        today = self._today()
        cost_usd = self._calc_usd(model, input_tokens, output_tokens)

        pipe = self._r.pipeline()

        # Daily total
        pipe.incrbyfloat(self._total_key(today), cost_usd)
        pipe.expire(self._total_key(today), _KEY_TTL)

        # Per-model counters
        pipe.incrby(self._model_tin_key(today, model), input_tokens)
        pipe.expire(self._model_tin_key(today, model), _KEY_TTL)
        pipe.incrby(self._model_tout_key(today, model), output_tokens)
        pipe.expire(self._model_tout_key(today, model), _KEY_TTL)
        pipe.incrbyfloat(self._model_cost_key(today, model), cost_usd)
        pipe.expire(self._model_cost_key(today, model), _KEY_TTL)

        results = pipe.execute()
        new_total = float(results[0])
        model_tokens = int(self._r.get(self._model_tin_key(today, model)) or 0) + int(
            self._r.get(self._model_tout_key(today, model)) or 0
        )

        logger.info(
            "COST | model={} | call=${:.6f} | day=${:.4f} | in={} out={}",
            model,
            cost_usd,
            new_total,
            input_tokens,
            output_tokens,
        )

        self._update_prometheus(new_total)
        self._check_and_alert(new_total, today)

        return CallCost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            call_cost_usd=round(cost_usd, 6),
            daily_total_usd=round(new_total, 6),
            daily_model_tokens=model_tokens,
        )

    def get_daily_summary(self) -> dict:
        """Return today's full spend breakdown. Used by GET /api/v1/costs."""
        s = get_settings()
        today = self._today()

        total = float(self._r.get(self._total_key(today)) or 0)
        ceiling = s.daily_cost_ceiling_usd

        models: dict[str, dict] = {}
        for model in PRICING:
            tin = int(self._r.get(self._model_tin_key(today, model)) or 0)
            tout = int(self._r.get(self._model_tout_key(today, model)) or 0)
            cost = float(self._r.get(self._model_cost_key(today, model)) or 0)
            if tin or tout:
                budget = self._model_token_budget(s, model)
                models[model] = {
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "tokens_total": tin + tout,
                    "cost_usd": round(cost, 6),
                    "token_budget": budget or None,
                    "pct_of_budget": round((tin + tout) / budget * 100, 1) if budget else None,
                }

        return {
            "date": today,
            "total_cost_usd": round(total, 6),
            "daily_ceiling_usd": ceiling or None,
            "pct_of_ceiling": round(total / ceiling * 100, 1) if ceiling else None,
            "remaining_usd": round(max(ceiling - total, 0), 6) if ceiling else None,
            "rate_limits": {
                "per_minute": s.rate_limit_per_minute,
                "per_hour": s.rate_limit_per_hour,
            },
            "models": models,
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _model_token_budget(s, model: str) -> int:
        """Return the per-model daily token budget (0 = unlimited)."""
        if "opus" in model:
            return getattr(s, "opus_daily_token_budget", 0)
        if "sonnet" in model:
            return s.sonnet_daily_token_budget
        if "haiku" in model:
            return s.haiku_daily_token_budget
        return 0

    def _update_prometheus(self, total_usd: float) -> None:
        try:
            from app.observability.prometheus_metrics import COST_DAILY_USD, COST_CEILING_USD

            COST_DAILY_USD.set(total_usd)
            COST_CEILING_USD.set(get_settings().daily_cost_ceiling_usd)
        except Exception:
            pass

    def _check_and_alert(self, total_usd: float, today: str) -> None:
        """Fire a Slack alert the first time each threshold is crossed today."""
        s = get_settings()
        if not s.daily_cost_ceiling_usd:
            return

        pct_used = total_usd / s.daily_cost_ceiling_usd
        for threshold in sorted(_parse_thresholds(s.budget_alert_thresholds)):
            if pct_used >= threshold:
                alert_key = self._alert_key(today, int(threshold * 100))
                if self._r.set(alert_key, "1", nx=True, ex=86_400):
                    self._send_slack_alert(total_usd, s.daily_cost_ceiling_usd, threshold, s)

    @staticmethod
    def _send_slack_alert(total_usd: float, ceiling: float, threshold: float, s) -> None:
        if not s.slack_bot_token:
            return
        pct = round(threshold * 100)
        over = threshold >= 1.0

        if over:
            header = f"🚨 *DAILY COST CEILING HIT — LLM calls are now BLOCKED*"
        elif threshold >= 0.8:
            header = f"⚠️ *Brain API spend at {pct}% of daily ceiling*"
        else:
            header = f"💰 *Brain API spend at {pct}% of daily ceiling*"

        text = (
            f"{header}\n"
            f"  Spent:     *${total_usd:.4f}* of *${ceiling:.2f}*\n"
            f"  Remaining: *${max(ceiling - total_usd, 0):.4f}*\n"
            f"  _Ceiling resets at midnight UTC. "
            f"Check `GET /api/v1/costs` for a full breakdown._"
        )

        try:
            from slack_sdk import WebClient  # sync client — safe in thread pool

            channel = s.slack_alert_channel or "sentinel-alerts"
            WebClient(token=s.slack_bot_token).chat_postMessage(
                channel=channel,
                text=text,
                mrkdwn=True,
            )
            logger.info("Budget alert posted to Slack | threshold={}%", pct)
        except Exception as exc:
            logger.error("Failed to post budget alert to Slack: {}", exc)


# ── Module-level singleton ────────────────────────────────────────────────────

cost_tracker = CostTracker()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_thresholds(raw: str) -> list[float]:
    """Parse comma-separated threshold string, e.g. '0.5,0.8,1.0'."""
    try:
        return [float(x.strip()) for x in raw.split(",") if x.strip()]
    except Exception:
        return [0.5, 0.8, 1.0]
