"""
ProposalExecutorSkill — translates ReflectionProposals → Goals with correct priority/metadata.

Trigger intent: proposal_status (list pending proposals from reflection)

Internal translation layer: takes a ReflectionProposal, decides on the right goal type,
priority, and metadata, then enqueues it.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.skills.base import BaseSkill, SkillResult
from app.skills.goal_queue_skill import get_goal_queue, Goal, compute_priority
from app.db.postgres import execute as pg_execute

logger = logging.getLogger(__name__)


@dataclass
class DispatchedProposal:
    proposal_title: str
    goal_id: str
    goal_type: str
    priority: float
    skill_hint: str


class ProposalExecutorSkill(BaseSkill):
    name = "proposal_executor"
    description = "Translates ReflectionProposals into queued Goals with the right priority and skill routing"
    trigger_intents = ["proposal_status"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        try:
            rows = await pg_execute(
                """
                SELECT detail->>'proposal_title' AS title,
                       detail->>'goal_id' AS goal_id,
                       detail->>'skill_hint' AS skill_hint,
                       created_at
                FROM sentinel_audit
                WHERE action = 'proposal_dispatched'
                ORDER BY created_at DESC
                LIMIT 20
                """
            )
            items = [dict(r) for r in (rows or [])]
            return SkillResult(context_data=json.dumps({"recent_proposals": items}))
        except Exception as e:
            return SkillResult(context_data=json.dumps({"error": str(e), "recent_proposals": []}))

    async def build_goal(self, proposal) -> DispatchedProposal:
        """
        Build and enqueue a Goal from a ReflectionProposal.
        proposal: ReflectionProposal (duck-typed)
        """
        from app.config import get_settings

        settings = get_settings()

        # Determine skill_hint and goal_type based on proposal type
        proposal_type = getattr(proposal, "type", "self_heal")

        if proposal_type == "prompt_change":
            skill_hint = "prompt_refinement"
            goal_type = "prompt_change"
        elif proposal_type == "new_skill":
            if not settings.sentinel_skill_evolution_enabled:
                logger.info(
                    "Skipping new_skill proposal '%s' — skill_evolution disabled",
                    proposal.title,
                )
                return DispatchedProposal(
                    proposal_title=proposal.title,
                    goal_id="",
                    goal_type="new_skill",
                    priority=0.0,
                    skill_hint="",
                )
            skill_hint = "skill_evolution"
            goal_type = "new_skill"
        elif proposal_type == "config_tune":
            skill_hint = "wake"
            goal_type = "config_tune"
        else:
            skill_hint = getattr(proposal, "skill", None) or "self_heal"
            goal_type = "self_heal"

        # Cap priority at auto max
        raw_priority = float(getattr(proposal, "priority", 5.0))
        capped_priority = min(raw_priority, settings.sentinel_goal_max_priority_auto)

        goal_id = str(uuid.uuid4())
        goal = Goal(
            id=goal_id,
            title=proposal.title,
            description=getattr(proposal, "detail", "") or getattr(proposal, "description", ""),
            created_by="skill:reflection",
            created_at=datetime.now(timezone.utc),
            priority=compute_priority(capped_priority),
            status="pending",
            skill_hint=skill_hint,
            metadata={
                "proposal_type": proposal_type,
                "estimated_impact": getattr(proposal, "estimated_impact", "medium"),
                "skill_file": getattr(proposal, "skill_file", None),
                "affected_skills": getattr(proposal, "affected_skills", []),
            },
        )

        queue = get_goal_queue()
        await queue.enqueue(goal)

        dispatched = DispatchedProposal(
            proposal_title=proposal.title,
            goal_id=goal_id,
            goal_type=goal_type,
            priority=capped_priority,
            skill_hint=skill_hint,
        )

        # Audit log
        try:
            await pg_execute(
                """
                INSERT INTO sentinel_audit (action, target, outcome, detail)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                "proposal_dispatched",
                proposal.title[:200],
                "goal_enqueued",
                json.dumps({
                    "proposal_title": proposal.title,
                    "goal_id": goal_id,
                    "skill_hint": skill_hint,
                    "priority": capped_priority,
                }),
            )
        except Exception as e:
            logger.warning("audit log for proposal_dispatched failed: %s", e)

        # Update Prometheus counter
        try:
            from app.observability.prometheus_metrics import PROPOSALS_DISPATCHED
            PROPOSALS_DISPATCHED.labels(type=proposal_type).inc()
        except Exception:
            pass

        logger.info(
            "ProposalExecutorSkill: dispatched '%s' → goal %s (skill_hint=%s, priority=%.1f)",
            proposal.title, goal_id, skill_hint, capped_priority,
        )
        return dispatched
