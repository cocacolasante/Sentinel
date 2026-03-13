"""
ReflectionSkill — Sonnet nightly pattern analysis.

Trigger intent: reflect

Analyzes last N hours of execution data and proposes self-improvements.
Gracefully handles empty execution log.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.skills.base import BaseSkill, SkillResult
from app.skills.observer_skill import get_observer

logger = logging.getLogger(__name__)


@dataclass
class ReflectionProposal:
    title: str
    description: str
    priority: float
    auto_actionable: bool
    affected_skills: list = field(default_factory=list)
    # Phase 5 extensions
    type: str = "self_heal"                  # "prompt_change" | "new_skill" | "config_tune" | "self_heal"
    skill_file: Optional[str] = None         # e.g. "app/skills/patch_audit_skill.py"
    detail: str = ""                         # detailed description for ProposalExecutorSkill
    estimated_impact: str = "medium"         # "low" | "medium" | "high"
    skill: Optional[str] = None             # skill name hint for goal routing


class ReflectionSkill(BaseSkill):
    name = "reflection"
    description = "Nightly reflection on execution patterns — proposes self-improvements"
    trigger_intents = ["reflect"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        from app.config import get_settings
        settings = get_settings()

        lookback_hours = int(params.get("lookback_hours", settings.sentinel_reflection_lookback_hours))
        report = await self.reflect(lookback_hours=lookback_hours)
        return SkillResult(context_data=json.dumps(report))

    async def reflect(self, lookback_hours: int = 24) -> dict:
        from app.config import get_settings

        settings = get_settings()
        observer = get_observer()

        # Gather data
        failures = await observer.failures_last_n_hours(lookback_hours)
        success_rates = await observer.success_rate_by_skill(lookback_hours)
        avg_tokens = await observer.avg_tokens_by_skill(lookback_hours)
        common_errors = await observer.most_common_errors(lookback_hours)

        # Graceful no-data path
        if not failures and not success_rates:
            report = {
                "lookback_hours": lookback_hours,
                "observation_count": 0,
                "proposals": [],
                "queued_goals": 0,
                "message": "No execution data found for this period",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._persist(report)
            return report

        # Get previous reports to avoid repeating proposals
        prev_proposals = await self._get_previous_proposals(3)

        # Call Sonnet for analysis
        report = await self._call_sonnet(
            failures=failures,
            success_rates=success_rates,
            avg_tokens=avg_tokens,
            common_errors=common_errors,
            prev_proposals=prev_proposals,
            lookback_hours=lookback_hours,
            settings=settings,
        )

        # Parse proposals into ReflectionProposal dataclasses
        raw_proposals = report.get("proposals", [])
        proposals = [
            ReflectionProposal(
                title=p.get("title", "Self-improvement proposal"),
                description=p.get("description", ""),
                priority=float(p.get("priority", 5.0)),
                auto_actionable=bool(p.get("auto_actionable", False)),
                affected_skills=p.get("affected_skills", []),
                type=p.get("type", "self_heal"),
                skill_file=p.get("skill_file"),
                detail=p.get("detail", p.get("description", "")),
                estimated_impact=p.get("estimated_impact", "medium"),
                skill=p.get("skill"),
            )
            for p in raw_proposals
        ]

        # Dispatch proposals via ProposalExecutorSkill
        await self.dispatch_proposals(proposals)

        report["queued_goals"] = sum(1 for p in proposals if p.auto_actionable)
        report["created_at"] = datetime.now(timezone.utc).isoformat()

        # Persist report
        await self._persist(report)

        # Post Slack summary
        try:
            from app.integrations.slack_notifier import post_alert_sync
            obs_count = report.get("observation_count", 0)
            queued = report["queued_goals"]
            top_titles = [p.title for p in proposals[:3]]
            msg = f"🧠 *Nightly Reflection* — {obs_count} observations, {len(proposals)} proposals, {queued} goals queued\n"
            if top_titles:
                msg += "Top proposals:\n" + "\n".join(f"  • {t}" for t in top_titles)
            post_alert_sync(msg, "sentinel-alerts")
        except Exception:
            pass

        return report

    async def dispatch_proposals(self, proposals: list[ReflectionProposal]) -> None:
        """Route auto_actionable proposals to ProposalExecutorSkill."""
        from app.skills.proposal_executor_skill import ProposalExecutorSkill
        executor = ProposalExecutorSkill()
        for p in proposals:
            if p.auto_actionable:
                try:
                    await executor.build_goal(p)
                except Exception as e:
                    logger.warning("dispatch_proposals: failed for '%s': %s", p.title, e)

    async def _call_sonnet(
        self,
        failures: list[dict],
        success_rates: dict,
        avg_tokens: dict,
        common_errors: list[dict],
        prev_proposals: list[str],
        lookback_hours: int,
        settings,
    ) -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        data_summary = {
            "lookback_hours": lookback_hours,
            "failure_count": len(failures),
            "skills_analyzed": list(success_rates.keys()),
            "success_rates": success_rates,
            "avg_tokens": avg_tokens,
            "top_errors": common_errors[:5],
            "recent_failures": failures[:10],
        }

        prev_block = ""
        if prev_proposals:
            prev_block = "\n\nDo NOT repeat these previously proposed improvements:\n" + "\n".join(
                f"  - {p}" for p in prev_proposals
            )

        prompt = f"""Analyze this Sentinel AI assistant execution data and identify patterns, issues, and improvements.
{json.dumps(data_summary, indent=2, default=str)}
{prev_block}

Return a JSON object:
{{
  "observation_count": <number>,
  "observations": ["<key pattern>", ...],
  "proposals": [
    {{
      "title": "<short title>",
      "description": "<what to improve and why>",
      "priority": <1-10>,
      "auto_actionable": <true if safe to auto-queue, false if needs human review>,
      "affected_skills": ["<skill_name>", ...],
      "type": "<prompt_change|new_skill|config_tune|self_heal>",
      "skill_file": "<app/skills/foo_skill.py or null>",
      "detail": "<detailed description for executor>",
      "estimated_impact": "<low|medium|high>",
      "skill": "<skill_name hint or null>"
    }}
  ]
}}
Return ONLY valid JSON."""

        try:
            resp = client.messages.create(
                model=settings.model_sonnet,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as e:
            logger.error("Sonnet reflection failed: %s", e)
            return {
                "observation_count": len(failures),
                "observations": [f"Analysis failed: {e}"],
                "proposals": [],
            }

    async def _persist(self, report: dict) -> None:
        try:
            from app.db.postgres import execute
            await execute(
                "INSERT INTO sentinel_reflection_reports (report) VALUES ($1::jsonb)",
                json.dumps(report),
            )
        except Exception as e:
            logger.warning("Failed to persist reflection report: %s", e)

    async def _get_previous_proposals(self, limit: int) -> list[str]:
        try:
            from app.db.postgres import execute
            rows = await execute(
                """
                SELECT report->'proposals' AS proposals
                FROM sentinel_reflection_reports
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            titles = []
            for row in (rows or []):
                proposals = row.get("proposals") or []
                if isinstance(proposals, str):
                    proposals = json.loads(proposals)
                for p in proposals:
                    if isinstance(p, dict) and p.get("title"):
                        titles.append(p["title"])
            return titles[:20]
        except Exception:
            return []
