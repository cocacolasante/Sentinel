"""
WakeSkill — 15-min heartbeat orchestrator.

Trigger intent: wake (also cron-triggered via Celery Beat every 15 min)

Checks goal queue, unacted alerts, scheduled tasks, open PRs.
Makes a decision and optionally executes.
Must complete < 5s for "sleep" path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from app.skills.base import BaseSkill, SkillResult
from app.skills.autonomy_gradient_skill import AutonomyGradientSkill
from app.skills.git_commit_skill import GitCommitSkill
from app.skills.github_skill import GitHubReadSkill

logger = logging.getLogger(__name__)

# Scheduled task UTC hours
_CERT_CHECK_HOUR = 2
_BACKUP_CHECK_HOUR = 2
_BACKUP_CHECK_MINUTE = 30
_DNS_AUDIT_HOUR = 4
_REFLECT_HOUR = 1
_BACKUP_RESTORE_DAY = 6   # Sunday = 6


@dataclass
class WakeDecision:
    action: Literal["sleep", "execute_goal", "handle_alert", "run_scheduled"]
    goal_id: Optional[str]
    reasoning: str


class WakeSkill(BaseSkill):
    name = "wake"
    description = "15-min heartbeat: check goal queue, alerts, and scheduled tasks"
    trigger_intents = ["wake"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        decision = await self.wake()
        return SkillResult(context_data=json.dumps({
            "action": decision.action,
            "goal_id": decision.goal_id,
            "reasoning": decision.reasoning,
        }))

    async def wake(self) -> WakeDecision:
        """
        Check all signals and make a decision.
        The "sleep" path must complete < 5s.
        """
        from app.observability.prometheus_metrics import WAKE_DECISIONS_TOTAL, GOAL_QUEUE_DEPTH

        # 1. Check goal queue
        pending_goals = await self._check_goal_queue()
        if pending_goals:
            decision = WakeDecision(
                action="execute_goal",
                goal_id=pending_goals[0].id,
                reasoning=f"Goal queue has {len(pending_goals)} pending goals; executing highest priority: {pending_goals[0].title[:60]}",
            )
            try:
                WAKE_DECISIONS_TOTAL.labels(decision="execute_goal").inc()
                GOAL_QUEUE_DEPTH.set(len(pending_goals))
            except Exception:
                pass
            await self._handle_execute_goal(pending_goals[0])
            return decision

        # 2. Check for unacted critical alerts
        has_alerts = await self._check_critical_alerts()
        if has_alerts:
            decision = WakeDecision(
                action="handle_alert",
                goal_id=None,
                reasoning="Unacted critical alerts detected in sentinel_audit",
            )
            try:
                WAKE_DECISIONS_TOTAL.labels(decision="handle_alert").inc()
            except Exception:
                pass
            return decision

        # 3. Check scheduled tasks
        scheduled = await self._check_scheduled_tasks()
        if scheduled:
            decision = WakeDecision(
                action="run_scheduled",
                goal_id=None,
                reasoning=f"Scheduled task due: {scheduled}",
            )
            try:
                WAKE_DECISIONS_TOTAL.labels(decision="run_scheduled").inc()
            except Exception:
                pass
            await self._run_scheduled(scheduled)

            # Phase 5: post-reflection dispatch_proposals
            if scheduled == "reflect":
                try:
                    from app.skills.reflection_skill import ReflectionSkill
                    # dispatch_proposals is called inside reflect() already,
                    # but this is the explicit hook for the wake loop
                    pass
                except Exception:
                    pass

            return decision

        # Phase 5: always run periodic checks (PR detection, daily snapshot)
        try:
            await self._check_scheduled_tasks_phase5()
        except Exception as e:
            logger.debug("_check_scheduled_tasks_phase5 failed: %s", e)

        # 4. Sleep
        decision = WakeDecision(
            action="sleep",
            goal_id=None,
            reasoning="No pending goals, alerts, or scheduled tasks",
        )
        try:
            WAKE_DECISIONS_TOTAL.labels(decision="sleep").inc()
        except Exception:
            pass
        return decision

    async def _check_goal_queue(self):
        try:
            from app.skills.goal_queue_skill import get_goal_queue
            queue = get_goal_queue()
            return await queue.peek(10)
        except Exception:
            return []

    async def _check_critical_alerts(self) -> bool:
        try:
            from app.db.postgres import execute
            rows = await execute(
                """
                SELECT id FROM sentinel_audit
                WHERE outcome = 'critical'
                  AND created_at > NOW() - INTERVAL '900 seconds'
                LIMIT 1
                """
            )
            return bool(rows)
        except Exception:
            return False

    async def _check_scheduled_tasks(self) -> Optional[str]:
        """Return the name of a due scheduled task, or None."""
        now = datetime.now(timezone.utc)
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()  # Monday=0, Sunday=6

        # Allow a 14-minute window (beat fires every 15 min)
        if hour == _CERT_CHECK_HOUR and minute < 15:
            return "cert_check"

        if hour == _BACKUP_CHECK_HOUR and minute >= _BACKUP_CHECK_MINUTE and minute < _BACKUP_CHECK_MINUTE + 15:
            if weekday == _BACKUP_RESTORE_DAY:
                return "backup_restore"
            return "backup_check"

        if hour == _DNS_AUDIT_HOUR and minute < 15:
            return "dns_audit"

        if hour == _REFLECT_HOUR and minute < 15:
            return "reflect"

        return None

    async def _run_scheduled(self, task_name: str) -> None:
        """Enqueue a goal for the scheduled task."""
        try:
            from app.skills.goal_queue_skill import get_goal_queue, Goal, compute_priority
            import uuid

            queue = get_goal_queue()
            title_map = {
                "cert_check": "Scheduled: Check SSL certificate expiry",
                "backup_check": "Scheduled: Verify backup recency and size",
                "backup_restore": "Scheduled: Verify backup with restore test",
                "dns_audit": "Scheduled: Audit DNS records for all domains",
                "reflect": "Scheduled: Nightly reflection on last 24h execution data",
            }
            skill_map = {
                "cert_check": "cert_check",
                "backup_check": "backup_check",
                "backup_restore": "backup_check",
                "dns_audit": "dns_audit",
                "reflect": "reflect",
            }
            goal = Goal(
                id=str(uuid.uuid4()),
                title=title_map.get(task_name, f"Scheduled: {task_name}"),
                description=f"Auto-enqueued by WakeSkill at {datetime.now(timezone.utc).isoformat()}",
                created_by="wake_loop",
                created_at=datetime.now(timezone.utc),
                priority=compute_priority(6.0),  # slightly above mid-priority
                status="pending",
                skill_hint=skill_map.get(task_name, task_name),
            )
            await queue.enqueue(goal)
        except Exception as e:
            logger.warning("Failed to enqueue scheduled task %s: %s", task_name, e)

    async def _check_scheduled_tasks_phase5(self) -> None:
        """Phase 5 additions to the scheduled task loop."""
        now = datetime.now(timezone.utc)

        # Daily snapshot + dashboard at 00:00 UTC (within 15-min window)
        if now.hour == 0 and now.minute < 15:
            try:
                from app.skills.autonomy_gradient_skill import AutonomyGradientSkill
                await AutonomyGradientSkill().snapshot_daily()
            except Exception as e:
                logger.warning("snapshot_daily failed: %s", e)
            try:
                from app.skills.self_improvement_dashboard_skill import SelfImprovementDashboardSkill
                await SelfImprovementDashboardSkill().post_daily_summary()
            except Exception as e:
                logger.warning("post_daily_summary failed: %s", e)

        # PR merge detection — check for recently merged sentinel-autofix PRs
        try:
            await self._check_merged_prs()
        except Exception as e:
            logger.debug("_check_merged_prs failed: %s", e)

    async def _check_merged_prs(self) -> None:
        """Detect recently merged sentinel/ PRs and fire post_merge_hook."""
        try:
            gh = GitHubReadSkill()
            result = await gh.execute(
                {
                    "repo": "cocacolasante/Sentinel",
                    "resource": "prs",
                    "state": "closed",
                    "label": "sentinel-autofix",
                    "since_minutes": 15,
                },
                "",
            )
            if result.is_error or not result.context_data:
                return

            import json
            data = json.loads(result.context_data)
            merged_prs = [pr for pr in (data if isinstance(data, list) else data.get("prs", [])) if pr.get("merged")]

            gc = GitCommitSkill()
            for pr in merged_prs:
                pr_number = pr.get("number")
                skill_name = (pr.get("metadata") or {}).get("evolved_skill") or pr.get("head", {}).get("ref", "").replace("sentinel/evolved-skill-", "").replace("sentinel/self-heal-", "")
                if pr_number:
                    await gc.post_merge_hook(pr_number, skill_name or None)
        except Exception as e:
            logger.debug("_check_merged_prs: %s", e)

    async def _handle_execute_goal(self, goal) -> None:
        """Plan and execute the top goal if autonomy is enabled."""
        from app.config import get_settings
        settings = get_settings()

        if not settings.brain_autonomy:
            logger.debug("brain_autonomy=False — goal %s stays pending", goal.id)
            return

        # Phase 5: consult AutonomyGradientSkill before executing
        try:
            ag = AutonomyGradientSkill()
            score = await ag.get_current()
            if not score.apply_gradient("goal_execution"):
                logger.info(
                    "AutonomyGradient score=%.2f below threshold — goal %s stays pending (needs_approval)",
                    score.score, goal.id,
                )
                try:
                    from app.skills.goal_queue_skill import get_goal_queue
                    await get_goal_queue().update_status(goal.id, "needs_approval")
                except Exception:
                    pass
                return
        except Exception as e:
            logger.debug("AutonomyGradientSkill check failed (continuing): %s", e)

        try:
            from app.skills.planner_skill import PlannerSkill
            from app.skills.executor_skill import ExecutorSkill

            planner = PlannerSkill()
            plan = await planner.plan(
                goal_id=goal.id,
                goal_title=goal.title,
                goal_description=goal.description,
            )

            executor = ExecutorSkill()
            await executor.execute_plan(plan, dry_run=False, goal_id=goal.id)

            # Mark goal complete
            from app.skills.goal_queue_skill import get_goal_queue
            await get_goal_queue().update_status(goal.id, "completed")
        except Exception as e:
            logger.error("Goal execution failed for %s: %s", goal.id, e)
            try:
                from app.skills.goal_queue_skill import get_goal_queue
                await get_goal_queue().update_status(goal.id, "failed")
            except Exception:
                pass
