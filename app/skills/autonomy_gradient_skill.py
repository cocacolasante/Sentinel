"""
AutonomyGradientSkill — dynamic approval-gate tuning based on execution history.

Trigger intent: autonomy_status

Computes a 0.0–1.0 score from rolling execution history. High score → auto-approve more;
low score → gate more actions behind Slack approval.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)

_SCORE_REDIS_KEY = "sentinel:autonomy_score"
_SCORE_TTL = 3600  # 1 hour
_OVERRIDE_KEY = "sentinel:autonomy_override"
_HISTORY_KEY_FMT = "sentinel:autonomy_history:{date}"
_HISTORY_TTL = 35 * 86400  # 35 days


@dataclass
class AutonomyScore:
    score: float                # 0.0 – 1.0
    success_rate: float
    avg_duration_ms: float
    sample_size: int
    recommendation: str         # "increase" | "maintain" | "decrease"
    reasoning: str

    def apply_gradient(self, decision_type: str) -> bool:
        """Return True if the decision should auto-execute given current score."""
        from app.config import get_settings
        settings = get_settings()

        # Emergency brake — uses synchronous Redis client (safe from any context)
        try:
            if _check_override_sync():
                return False
        except Exception:
            pass

        return self.score >= settings.sentinel_autonomy_high_threshold


def _check_override_sync() -> bool:
    """Synchronous check for emergency override key."""
    try:
        import redis as redis_lib
        from app.config import get_settings
        settings = get_settings()
        r = redis_lib.from_url(
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
            decode_responses=True,
        )
        val = r.get(_OVERRIDE_KEY)
        if val is not None:
            try:
                from app.integrations.slack_notifier import post_dm_sync
                post_dm_sync("🚨 *Autonomy override active* — all auto-execution halted. Remove `sentinel:autonomy_override` Redis key to re-enable.")
            except Exception:
                pass
            return True
        return False
    except Exception:
        return False


class AutonomyGradientSkill(BaseSkill):
    name = "autonomy_gradient"
    description = "Check and compute the dynamic autonomy gradient score (0–1) based on execution history"
    trigger_intents = ["autonomy_status"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        score = await self.get_current()
        return SkillResult(context_data=json.dumps({
            "score": score.score,
            "success_rate": score.success_rate,
            "avg_duration_ms": score.avg_duration_ms,
            "sample_size": score.sample_size,
            "recommendation": score.recommendation,
            "reasoning": score.reasoning,
        }))

    async def compute(self, lookback_hours: int = 24) -> AutonomyScore:
        """Compute autonomy score from execution history."""
        from app.config import get_settings
        settings = get_settings()

        try:
            from app.skills.observer_skill import get_observer
            observer = get_observer()
            success_rates = await observer.success_rate_by_skill(lookback_hours)
            avg_tokens = await observer.avg_tokens_by_skill(lookback_hours)
        except Exception as e:
            logger.warning("AutonomyGradientSkill: observer failed: %s", e)
            success_rates = {}
            avg_tokens = {}

        sample_size = len(success_rates)

        if sample_size < settings.sentinel_autonomy_min_sample_size:
            score = AutonomyScore(
                score=0.5,
                success_rate=0.5,
                avg_duration_ms=0.0,
                sample_size=sample_size,
                recommendation="maintain",
                reasoning=f"Insufficient samples ({sample_size} < {settings.sentinel_autonomy_min_sample_size}) — defaulting to 0.5",
            )
            await self._cache_score(score)
            return score

        # Compute success rate component (weight 0.6)
        overall_success = sum(success_rates.values()) / len(success_rates) if success_rates else 0.5
        success_component = overall_success * 0.6

        # Compute latency score component (weight 0.2): tokens as proxy for latency
        # Lower tokens = better latency = higher score
        avg_tok_values = list(avg_tokens.values())
        if avg_tok_values:
            avg_tok = sum(avg_tok_values) / len(avg_tok_values)
            # Normalize: 0 tokens = 1.0, 4000+ tokens = 0.0
            latency_score = max(0.0, 1.0 - avg_tok / 4000.0)
        else:
            latency_score = 0.5
        latency_component = latency_score * 0.2

        # Error diversity component (weight 0.2): fewer distinct failing skills = higher score
        failing_skills = sum(1 for v in success_rates.values() if v < 0.5)
        error_diversity = max(0.0, 1.0 - failing_skills / max(len(success_rates), 1))
        error_component = error_diversity * 0.2

        composite = success_component + latency_component + error_component
        composite = max(0.0, min(1.0, composite))

        if composite >= settings.sentinel_autonomy_high_threshold:
            recommendation = "increase"
        elif composite <= settings.sentinel_autonomy_low_threshold:
            recommendation = "decrease"
        else:
            recommendation = "maintain"

        score = AutonomyScore(
            score=composite,
            success_rate=overall_success,
            avg_duration_ms=sum(avg_tok_values) / len(avg_tok_values) if avg_tok_values else 0.0,
            sample_size=sample_size,
            recommendation=recommendation,
            reasoning=(
                f"success={overall_success:.2f}, latency={latency_score:.2f}, "
                f"error_diversity={error_diversity:.2f} → composite={composite:.2f}"
            ),
        )

        await self._cache_score(score)

        # Update Prometheus
        try:
            from app.observability.prometheus_metrics import AUTONOMY_SCORE
            AUTONOMY_SCORE.set(composite)
        except Exception:
            pass

        return score

    async def get_current(self) -> AutonomyScore:
        """Return cached score from Redis, or recompute on miss."""
        try:
            from app.db.redis import get_redis
            redis = await get_redis()
            raw = await redis.get(_SCORE_REDIS_KEY)
            if raw:
                d = json.loads(raw)
                return AutonomyScore(**d)
        except Exception:
            pass
        return await self.compute()

    async def snapshot_daily(self) -> None:
        """Write today's score to Redis for trend analysis (35d TTL)."""
        score = await self.get_current()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = _HISTORY_KEY_FMT.format(date=today)
        try:
            from app.db.redis import get_redis
            redis = await get_redis()
            await redis.setex(key, _HISTORY_TTL, json.dumps({
                "score": score.score,
                "success_rate": score.success_rate,
                "sample_size": score.sample_size,
                "recommendation": score.recommendation,
            }))
        except Exception as e:
            logger.warning("snapshot_daily failed: %s", e)

    async def _cache_score(self, score: AutonomyScore) -> None:
        try:
            from app.db.redis import get_redis
            redis = await get_redis()
            await redis.setex(_SCORE_REDIS_KEY, _SCORE_TTL, json.dumps({
                "score": score.score,
                "success_rate": score.success_rate,
                "avg_duration_ms": score.avg_duration_ms,
                "sample_size": score.sample_size,
                "recommendation": score.recommendation,
                "reasoning": score.reasoning,
            }))
        except Exception as e:
            logger.warning("Failed to cache autonomy score: %s", e)
