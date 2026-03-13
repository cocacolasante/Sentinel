"""
SelfImprovementDashboardSkill — aggregates Phase 4/5 metrics for Slack/Grafana summary.

Trigger intent: self_improvement_status
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)

_HISTORY_KEY_FMT = "sentinel:autonomy_history:{date}"


@dataclass
class SelfImprovementReport:
    period_hours: int
    autonomy_score: float
    autonomy_trend: str              # "improving" | "stable" | "declining"
    goals_completed: int
    goals_failed: int
    proposals_dispatched: int
    proposals_by_type: dict = field(default_factory=dict)
    skills_evolved: int = 0
    prompt_ab_winners: list[str] = field(default_factory=list)
    top_failing_skills: list[str] = field(default_factory=list)
    reflection_summary: str = ""


class SelfImprovementDashboardSkill(BaseSkill):
    name = "self_improvement_dashboard"
    description = "Show a Phase 4/5 self-improvement dashboard summary"
    trigger_intents = ["self_improvement_status"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        period_hours = int(params.get("period_hours", 24))
        report = await self.generate(period_hours)
        return SkillResult(context_data=json.dumps({
            "period_hours": report.period_hours,
            "autonomy_score": report.autonomy_score,
            "autonomy_trend": report.autonomy_trend,
            "goals_completed": report.goals_completed,
            "goals_failed": report.goals_failed,
            "proposals_dispatched": report.proposals_dispatched,
            "proposals_by_type": report.proposals_by_type,
            "skills_evolved": report.skills_evolved,
            "prompt_ab_winners": report.prompt_ab_winners,
            "top_failing_skills": report.top_failing_skills,
            "reflection_summary": report.reflection_summary,
        }))

    async def generate(self, period_hours: int = 24) -> SelfImprovementReport:
        """Aggregate all Phase 4/5 data into a single report."""
        goals_completed, goals_failed, top_failing = await self._query_goals(period_hours)
        proposals_dispatched, proposals_by_type = await self._query_proposals(period_hours)
        prompt_ab_winners = await self._query_ab_winners()
        autonomy_score, autonomy_trend = await self._read_autonomy_trend()

        # Estimate skills evolved from audit log
        skills_evolved = await self._count_evolved_skills(period_hours)

        # Generate reflection summary if there is data
        reflection_summary = ""
        if goals_completed + goals_failed + proposals_dispatched > 0:
            reflection_summary = await self._generate_summary(
                goals_completed, goals_failed, proposals_dispatched,
                autonomy_score, autonomy_trend, period_hours,
            )

        return SelfImprovementReport(
            period_hours=period_hours,
            autonomy_score=autonomy_score,
            autonomy_trend=autonomy_trend,
            goals_completed=goals_completed,
            goals_failed=goals_failed,
            proposals_dispatched=proposals_dispatched,
            proposals_by_type=proposals_by_type,
            skills_evolved=skills_evolved,
            prompt_ab_winners=prompt_ab_winners,
            top_failing_skills=top_failing,
            reflection_summary=reflection_summary,
        )

    async def post_daily_summary(self) -> None:
        """Called by WakeSkill at 00:00 UTC — generate and post to Slack."""
        report = await self.generate(24)

        msg = (
            f"📊 *Self-Improvement Daily Report*\n"
            f"• Autonomy score: {report.autonomy_score:.2f} ({report.autonomy_trend})\n"
            f"• Goals: ✅ {report.goals_completed} completed, ❌ {report.goals_failed} failed\n"
            f"• Proposals dispatched: {report.proposals_dispatched}\n"
            f"• Skills evolved: {report.skills_evolved}\n"
            f"• Prompt A/B winners: {', '.join(report.prompt_ab_winners) or 'none'}\n"
        )
        if report.top_failing_skills:
            msg += f"• Top failing skills: {', '.join(report.top_failing_skills[:3])}\n"
        if report.reflection_summary:
            msg += f"\n_{report.reflection_summary}_"

        try:
            from app.config import get_settings
            from app.integrations.slack_notifier import post_alert_sync
            settings = get_settings()
            post_alert_sync(msg, settings.slack_alert_channel)
        except Exception as e:
            logger.warning("post_daily_summary Slack failed: %s", e)

        # Update Prometheus
        try:
            from app.observability.prometheus_metrics import AUTONOMY_SCORE, SELF_IMPROVEMENT_CYCLE
            AUTONOMY_SCORE.set(report.autonomy_score)
            outcome = "success" if report.goals_failed == 0 else "partial"
            SELF_IMPROVEMENT_CYCLE.labels(outcome=outcome).inc()
        except Exception:
            pass

    async def _query_goals(self, period_hours: int) -> tuple[int, int, list[str]]:
        try:
            from app.db.postgres import execute
            rows = await execute(
                """
                SELECT skill, status, COUNT(*) as cnt
                FROM sentinel_execution_log
                WHERE started_at > NOW() - ($1 || ' hours')::INTERVAL
                GROUP BY skill, status
                """,
                str(period_hours),
            )
            completed = sum(r["cnt"] for r in (rows or []) if r["status"] == "success")
            failed = sum(r["cnt"] for r in (rows or []) if r["status"] == "failed")
            failing_skills = [r["skill"] for r in (rows or []) if r["status"] == "failed"]
            return completed, failed, failing_skills[:5]
        except Exception as e:
            logger.debug("_query_goals failed: %s", e)
            return 0, 0, []

    async def _query_proposals(self, period_hours: int) -> tuple[int, dict]:
        try:
            from app.db.postgres import execute
            rows = await execute(
                """
                SELECT detail->>'proposal_type' AS ptype, COUNT(*) as cnt
                FROM sentinel_audit
                WHERE action = 'proposal_dispatched'
                  AND created_at > NOW() - ($1 || ' hours')::INTERVAL
                GROUP BY ptype
                """,
                str(period_hours),
            )
            by_type: dict = {}
            total = 0
            for r in (rows or []):
                ptype = r["ptype"] or "unknown"
                by_type[ptype] = r["cnt"]
                total += r["cnt"]
            return total, by_type
        except Exception as e:
            logger.debug("_query_proposals failed: %s", e)
            return 0, {}

    async def _query_ab_winners(self) -> list[str]:
        try:
            from app.db.postgres import execute
            rows = await execute(
                """
                SELECT DISTINCT skill_name
                FROM sentinel_prompt_ab_tests
                WHERE variant = 'control'
                  AND last_updated > NOW() - INTERVAL '48 hours'
                  AND is_active = TRUE
                """
            )
            return [r["skill_name"] for r in (rows or [])]
        except Exception:
            return []

    async def _count_evolved_skills(self, period_hours: int) -> int:
        try:
            from app.db.postgres import execute
            rows = await execute(
                """
                SELECT COUNT(*) AS cnt
                FROM sentinel_audit
                WHERE action = 'post_merge_hook'
                  AND created_at > NOW() - ($1 || ' hours')::INTERVAL
                """,
                str(period_hours),
            )
            return (rows[0]["cnt"] if rows else 0) or 0
        except Exception:
            return 0

    async def _read_autonomy_trend(self) -> tuple[float, str]:
        """Read today's and yesterday's autonomy score from Redis."""
        try:
            from app.db.redis import get_redis
            redis = await get_redis()
            now = datetime.now(timezone.utc)
            today_key = _HISTORY_KEY_FMT.format(date=now.strftime("%Y-%m-%d"))
            yesterday = now.replace(day=now.day - 1) if now.day > 1 else now
            yesterday_key = _HISTORY_KEY_FMT.format(date=yesterday.strftime("%Y-%m-%d"))

            today_raw = await redis.get(today_key)
            yesterday_raw = await redis.get(yesterday_key)

            today_score = json.loads(today_raw)["score"] if today_raw else None
            yesterday_score = json.loads(yesterday_raw)["score"] if yesterday_raw else None

            if today_score is None:
                from app.skills.autonomy_gradient_skill import AutonomyGradientSkill
                ag = AutonomyGradientSkill()
                as_ = await ag.get_current()
                today_score = as_.score

            if yesterday_score is None:
                trend = "stable"
            elif today_score > yesterday_score + 0.05:
                trend = "improving"
            elif today_score < yesterday_score - 0.05:
                trend = "declining"
            else:
                trend = "stable"

            return today_score, trend
        except Exception as e:
            logger.debug("_read_autonomy_trend failed: %s", e)
            return 0.5, "stable"

    async def _generate_summary(
        self,
        goals_completed: int,
        goals_failed: int,
        proposals_dispatched: int,
        autonomy_score: float,
        autonomy_trend: str,
        period_hours: int,
    ) -> str:
        """Use Haiku to write a 2-sentence reflection summary."""
        try:
            import anthropic
            from app.config import get_settings
            settings = get_settings()
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            prompt = (
                f"Summarize this Sentinel AI self-improvement period in exactly 2 sentences:\n"
                f"Period: last {period_hours}h\n"
                f"Goals completed: {goals_completed}, failed: {goals_failed}\n"
                f"Proposals dispatched: {proposals_dispatched}\n"
                f"Autonomy score: {autonomy_score:.2f} ({autonomy_trend})\n"
                f"Be concise and factual."
            )
            resp = client.messages.create(
                model=settings.model_haiku,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception:
            return ""
