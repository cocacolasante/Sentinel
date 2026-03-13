"""
ExecutorSkill — topological DAG executor.

Trigger intent: wake (also called by WakeSkill internally)

Executes an ExecutionPlan step by step, respecting dependencies and
failure policies. Records all execution events via ObserverSkill.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from app.skills.base import BaseSkill, SkillResult
from app.skills.planner_skill import ExecutionPlan, PlanStep, _topological_sort

logger = logging.getLogger(__name__)


class ExecutorSkill(BaseSkill):
    name = "executor"
    description = "Execute a planned DAG of skill steps with dependency resolution"
    trigger_intents: list[str] = []  # called internally by WakeSkill

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        plan_dict = params.get("plan")
        if not plan_dict:
            return SkillResult(context_data="No plan provided", is_error=True)

        plan = ExecutionPlan(**plan_dict)
        dry_run = params.get("dry_run", True)
        result = await self.execute_plan(plan, dry_run=dry_run)
        return SkillResult(context_data=json.dumps(result))

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        dry_run: bool = True,
        goal_id: Optional[str] = None,
    ) -> dict:
        """
        Execute plan steps in topological order.
        Independent steps run concurrently (max sentinel_max_concurrent_steps).
        """
        from app.config import get_settings
        from app.skills.observer_skill import get_observer
        from app.brain.dispatcher import _build_skill_registry

        settings = get_settings()

        # Guard: force dry_run unless autonomy is enabled
        if not settings.brain_autonomy:
            dry_run = True

        try:
            sorted_steps = _topological_sort(plan.steps)
        except Exception as e:
            return {"status": "error", "message": str(e)}

        observer = get_observer()
        plan_id = str(uuid.uuid4())
        reg = _build_skill_registry()

        completed_steps: set[str] = set()
        failed_steps: set[str] = set()
        aborted = False
        results: list[dict] = []

        sem = asyncio.Semaphore(settings.sentinel_max_concurrent_steps)

        async def _execute_step(step: PlanStep) -> dict:
            nonlocal aborted

            if aborted:
                return {"step_id": step.step_id, "status": "skipped", "reason": "plan aborted"}

            # Wait for dependencies
            for dep in step.depends_on:
                if dep in failed_steps and step.on_failure == "abort":
                    aborted = True
                    return {"step_id": step.step_id, "status": "skipped", "reason": f"dependency {dep} failed"}

            async with sem:
                start_ms = int(time.time() * 1000)
                skill_result = None

                try:
                    skill = reg._skills.get(step.skill)
                    if not skill:
                        raise ValueError(f"Skill {step.skill} not found in registry")

                    if dry_run:
                        # Describe what would happen without executing
                        skill_result_str = f"[DRY RUN] Would execute {step.skill} with params: {json.dumps(step.parameters)}"
                        status = "dry_run"
                    else:
                        # Execute the skill
                        sr = await skill.execute(step.parameters, original_message="")
                        skill_result_str = sr.context_data[:500] if sr else ""
                        status = "failed" if (sr and sr.is_error) else "success"

                    duration_ms = int(time.time() * 1000) - start_ms
                    summary = await observer.generate_summary(
                        status=status,
                        skill_name=step.skill,
                        parameters=step.parameters,
                    )

                    from app.skills.observer_skill import ExecutionEvent
                    await observer.record(ExecutionEvent(
                        goal_id=goal_id or plan.goal_id,
                        plan_id=plan_id,
                        step_id=step.step_id,
                        skill_name=step.skill,
                        status=status,
                        duration_ms=duration_ms,
                        parameters=step.parameters,
                        result_summary=summary,
                    ))

                    if status in ("failed",) and not dry_run:
                        failed_steps.add(step.step_id)
                        if step.on_failure == "abort":
                            aborted = True
                            _alert_failure(step, skill_result_str)
                        elif step.on_failure == "escalate":
                            await _escalate(step, skill_result_str)
                    else:
                        completed_steps.add(step.step_id)

                    return {
                        "step_id": step.step_id,
                        "skill": step.skill,
                        "status": status,
                        "summary": summary,
                        "duration_ms": duration_ms,
                    }

                except Exception as e:
                    duration_ms = int(time.time() * 1000) - start_ms
                    failed_steps.add(step.step_id)
                    if step.on_failure == "abort":
                        aborted = True

                    from app.skills.observer_skill import ExecutionEvent
                    await observer.record(ExecutionEvent(
                        goal_id=goal_id or plan.goal_id,
                        plan_id=plan_id,
                        step_id=step.step_id,
                        skill_name=step.skill,
                        status="failed",
                        duration_ms=duration_ms,
                        error_message=str(e),
                        error_type=type(e).__name__,
                        parameters=step.parameters,
                    ))

                    return {
                        "step_id": step.step_id,
                        "skill": step.skill,
                        "status": "failed",
                        "error": str(e),
                        "duration_ms": duration_ms,
                    }

        # Group independent steps and execute in waves
        for step in sorted_steps:
            if aborted:
                break
            step_result = await _execute_step(step)
            results.append(step_result)

        overall_status = "completed" if not failed_steps else "partial"
        if aborted:
            overall_status = "aborted"

        summary = {
            "plan_id": plan_id,
            "goal_id": goal_id or plan.goal_id,
            "overall_status": overall_status,
            "dry_run": dry_run,
            "steps_completed": len(completed_steps),
            "steps_failed": len(failed_steps),
            "step_results": results,
        }

        # Post Slack summary
        try:
            from app.integrations.slack_notifier import post_alert_sync
            emoji = "✅" if overall_status == "completed" else ("⚠️" if overall_status == "partial" else "🛑")
            msg = f"{emoji} *Plan Execution {overall_status.upper()}*\n"
            msg += f"Plan: `{plan_id}` | Goal: `{goal_id or plan.goal_id}`\n"
            msg += f"Steps: {len(completed_steps)} completed, {len(failed_steps)} failed | dry_run={dry_run}"
            post_alert_sync(msg, "sentinel-alerts")
        except Exception:
            pass

        return summary


def _alert_failure(step: PlanStep, output: str) -> None:
    try:
        from app.integrations.slack_notifier import post_alert_sync
        post_alert_sync(
            f"🛑 *Plan aborted* — step `{step.step_id}` skill `{step.skill}` failed\n"
            f"```{output[:300]}```",
            "sentinel-alerts",
        )
    except Exception:
        pass


async def _escalate(step: PlanStep, output: str) -> None:
    """Store step in Redis pending approval and post Slack prompt."""
    try:
        import uuid
        from app.db.redis import get_redis
        from app.integrations.slack_notifier import post_alert_sync

        approval_id = str(uuid.uuid4())
        redis = await get_redis()
        await redis.setex(
            f"sentinel:pending_approval:{approval_id}",
            86400,
            json.dumps({"step_id": step.step_id, "skill": step.skill, "parameters": step.parameters}),
        )
        post_alert_sync(
            f"⚠️ *Step requires approval* — ID `{approval_id}`\n"
            f"Skill: `{step.skill}` | step_id=`{step.step_id}`\n"
            f"Reply `confirm {approval_id}` to proceed.",
            "sentinel-alerts",
        )
    except Exception as e:
        logger.error("Escalation failed: %s", e)
