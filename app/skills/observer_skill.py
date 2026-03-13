"""
ObserverSkill — execution event logger.

Internal singleton — not user-triggerable.
Records every skill execution to sentinel_execution_log.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)

_SUMMARY_CACHE_TTL = 3600  # 1 hour

_INSTANCE: Optional["ObserverSkill"] = None


def get_observer() -> "ObserverSkill":
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ObserverSkill()
    return _INSTANCE


@dataclass
class ExecutionEvent:
    skill_name: str
    status: str
    goal_id: Optional[str] = None
    plan_id: Optional[str] = None
    step_id: Optional[str] = None
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    error_message: Optional[str] = None
    error_type: Optional[str] = None
    parameters: dict = field(default_factory=dict)
    result_summary: Optional[str] = None


class ObserverSkill(BaseSkill):
    name = "observer"
    description = "Internal execution event logger — not user-triggerable"
    trigger_intents: list[str] = []

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        return SkillResult(context_data="ObserverSkill is internal-only")

    async def record(self, event: ExecutionEvent) -> None:
        """Insert execution event into sentinel_execution_log and update metrics."""
        try:
            from app.db.postgres import execute
            await execute(
                """
                INSERT INTO sentinel_execution_log
                    (goal_id, plan_id, step_id, skill_name, model_used, status,
                     input_tokens, output_tokens, duration_ms, error_message,
                     error_type, parameters, result_summary)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)
                """,
                event.goal_id, event.plan_id, event.step_id,
                event.skill_name, event.model_used, event.status,
                event.input_tokens, event.output_tokens, event.duration_ms,
                event.error_message, event.error_type,
                json.dumps(event.parameters), event.result_summary,
            )
        except Exception as e:
            logger.warning("ObserverSkill.record failed: %s", e)

        try:
            from app.observability.prometheus_metrics import SKILL_EXECUTIONS_TOTAL, SKILL_DURATION_MS
            SKILL_EXECUTIONS_TOTAL.labels(skill=event.skill_name, status=event.status).inc()
            if event.duration_ms > 0:
                SKILL_DURATION_MS.labels(skill=event.skill_name).observe(event.duration_ms)
        except Exception:
            pass

    async def generate_summary(
        self,
        status: str,
        skill_name: str,
        parameters: dict,
        error_message: Optional[str] = None,
    ) -> str:
        """Generate a 1-sentence summary via Haiku. Caches in Redis."""
        from app.config import get_settings
        settings = get_settings()

        # Build cache key
        key_data = f"{status}:{skill_name}:{json.dumps(parameters, sort_keys=True)}:{error_message or ''}"
        cache_hash = hashlib.sha256(key_data.encode()).hexdigest()[:16]
        cache_key = f"sentinel:summary_cache:{cache_hash}"

        try:
            from app.db.redis import get_redis
            redis = await get_redis()
            cached = await redis.get(cache_key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else cached
        except Exception:
            pass

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            prompt = (
                f"Summarize in one sentence: skill={skill_name} status={status} "
                f"params={json.dumps(parameters)[:200]}"
            )
            if error_message:
                prompt += f" error={error_message[:100]}"
            resp = client.messages.create(
                model=settings.model_haiku,
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = resp.content[0].text.strip()
        except Exception as e:
            summary = f"{skill_name} {status}"
            logger.debug("Summary generation failed: %s", e)

        try:
            from app.db.redis import get_redis
            redis = await get_redis()
            await redis.setex(cache_key, _SUMMARY_CACHE_TTL, summary)
        except Exception:
            pass

        return summary

    async def failures_last_n_hours(self, hours: int = 24) -> list[dict]:
        from app.db.postgres import execute
        rows = await execute(
            """
            SELECT skill_name, error_message, error_type, created_at
            FROM sentinel_execution_log
            WHERE status = 'failed'
              AND created_at > NOW() - ($1 || ' hours')::INTERVAL
            ORDER BY created_at DESC
            LIMIT 100
            """,
            str(hours),
        )
        return [dict(r) for r in (rows or [])]

    async def success_rate_by_skill(self, hours: int = 24) -> dict[str, float]:
        from app.db.postgres import execute
        rows = await execute(
            """
            SELECT skill_name,
                   COUNT(*) FILTER (WHERE status = 'success') AS successes,
                   COUNT(*) AS total
            FROM sentinel_execution_log
            WHERE created_at > NOW() - ($1 || ' hours')::INTERVAL
            GROUP BY skill_name
            """,
            str(hours),
        )
        result = {}
        for r in (rows or []):
            total = r["total"] or 0
            if total > 0:
                result[r["skill_name"]] = round(r["successes"] / total, 3)
        return result

    async def avg_tokens_by_skill(self, hours: int = 24) -> dict[str, dict]:
        from app.db.postgres import execute
        rows = await execute(
            """
            SELECT skill_name,
                   AVG(input_tokens) AS avg_input,
                   AVG(output_tokens) AS avg_output
            FROM sentinel_execution_log
            WHERE created_at > NOW() - ($1 || ' hours')::INTERVAL
              AND input_tokens IS NOT NULL
            GROUP BY skill_name
            """,
            str(hours),
        )
        result = {}
        for r in (rows or []):
            result[r["skill_name"]] = {
                "avg_input": round(float(r["avg_input"] or 0), 1),
                "avg_output": round(float(r["avg_output"] or 0), 1),
            }
        return result

    async def most_common_errors(self, hours: int = 24, limit: int = 10) -> list[dict]:
        from app.db.postgres import execute
        rows = await execute(
            """
            SELECT error_type, error_message, COUNT(*) AS count
            FROM sentinel_execution_log
            WHERE status = 'failed'
              AND created_at > NOW() - ($1 || ' hours')::INTERVAL
            GROUP BY error_type, error_message
            ORDER BY count DESC
            LIMIT $2
            """,
            str(hours), limit,
        )
        return [dict(r) for r in (rows or [])]
