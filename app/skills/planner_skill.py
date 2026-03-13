"""
PlannerSkill — Opus + extended thinking DAG planner.

Trigger intent: plan_goal

Produces a validated ExecutionPlan with topological ordering.
Writes plan to sentinel_execution_plans.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import BaseModel, field_validator

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)


class PlanStep(BaseModel):
    step_id: str
    skill: str
    description: str
    depends_on: list[str] = []
    model_tier: str = "sonnet"   # "haiku" | "sonnet" | "opus"
    estimated_input_tokens: int = 1000
    estimated_output_tokens: int = 500
    timeout_seconds: int = 60
    on_failure: str = "abort"     # "abort" | "skip" | "escalate"
    parameters: dict = {}


class ExecutionPlan(BaseModel):
    goal_id: str
    steps: list[PlanStep]
    estimated_total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    confidence: float = 0.8


class PlanError(Exception):
    pass


def _topological_sort(steps: list[PlanStep]) -> list[PlanStep]:
    """Kahn's algorithm — raises PlanError on cycle."""
    step_map = {s.step_id: s for s in steps}
    in_degree = {s.step_id: 0 for s in steps}
    for s in steps:
        for dep in s.depends_on:
            if dep not in step_map:
                raise PlanError(f"Step {s.step_id} depends on unknown step {dep}")
            in_degree[s.step_id] = in_degree.get(s.step_id, 0) + 1

    queue = [s for s in steps if in_degree[s.step_id] == 0]
    sorted_steps = []
    while queue:
        node = queue.pop(0)
        sorted_steps.append(node)
        for s in steps:
            if node.step_id in s.depends_on:
                in_degree[s.step_id] -= 1
                if in_degree[s.step_id] == 0:
                    queue.append(s)

    if len(sorted_steps) != len(steps):
        raise PlanError("Circular dependency detected in execution plan")
    return sorted_steps


class PlannerSkill(BaseSkill):
    name = "planner"
    description = "Opus-powered DAG planner — decompose a goal into executable steps"
    trigger_intents = ["plan_goal"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        goal_id = params.get("goal_id", "")
        goal_title = params.get("title", original_message[:200])
        goal_description = params.get("description", "")

        try:
            plan = await self.plan(goal_id=goal_id, goal_title=goal_title, goal_description=goal_description)
            return SkillResult(context_data=json.dumps(plan.model_dump()))
        except PlanError as e:
            return SkillResult(
                context_data=json.dumps({"error": str(e)}),
                is_error=True,
            )

    async def plan(
        self,
        goal_id: str,
        goal_title: str,
        goal_description: str,
        available_skills: Optional[list[str]] = None,
    ) -> ExecutionPlan:
        """
        Call Opus with extended thinking to produce an ExecutionPlan.
        Validates plan, retries once on failure.
        """
        from app.config import get_settings
        settings = get_settings()

        if available_skills is None:
            available_skills = await self._get_available_skills()

        errors: list[str] = []
        for attempt in range(2):
            try:
                plan = await self._call_opus(
                    goal_id=goal_id,
                    goal_title=goal_title,
                    goal_description=goal_description,
                    available_skills=available_skills,
                    prior_errors=errors,
                    settings=settings,
                )
                # Validate
                await self._validate(plan, available_skills, settings)
                await self._persist(plan)
                return plan
            except PlanError as e:
                if attempt == 0:
                    errors.append(str(e))
                    logger.warning("Plan attempt 1 failed (%s) — retrying", e)
                else:
                    raise

        raise PlanError("Planning failed after 2 attempts")

    async def _call_opus(
        self,
        goal_id: str,
        goal_title: str,
        goal_description: str,
        available_skills: list[str],
        prior_errors: list[str],
        settings,
    ) -> ExecutionPlan:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        skills_str = "\n".join(f"  - {s}" for s in available_skills)
        error_block = ""
        if prior_errors:
            error_block = "\n\nPrevious attempt failed with these errors (fix them):\n" + "\n".join(
                f"  - {e}" for e in prior_errors
            )

        system_prompt = (
            "You are a senior software architect planning the execution of an AI assistant goal. "
            "Produce a valid JSON ExecutionPlan with steps that use only the available skills listed. "
            "Never reference unavailable skills. Ensure no circular dependencies. "
            "Return ONLY valid JSON matching the schema — no prose."
        )

        user_prompt = f"""Goal: {goal_title}
Description: {goal_description}
Goal ID: {goal_id or 'new-goal'}

Available skills:
{skills_str}
{error_block}

Return a JSON object matching this schema exactly:
{{
  "goal_id": "{goal_id or 'new-goal'}",
  "steps": [
    {{
      "step_id": "step_1",
      "skill": "<skill_name>",
      "description": "What this step does",
      "depends_on": [],
      "model_tier": "haiku|sonnet|opus",
      "estimated_input_tokens": 1000,
      "estimated_output_tokens": 500,
      "timeout_seconds": 60,
      "on_failure": "abort|skip|escalate",
      "parameters": {{}}
    }}
  ],
  "estimated_total_tokens": 5000,
  "estimated_cost_usd": 0.05,
  "confidence": 0.85
}}"""

        resp = client.messages.create(
            model=settings.model_opus,
            max_tokens=4096,
            thinking={"type": "enabled", "budget_tokens": 6144},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Extract text from response
        raw = ""
        for block in resp.content:
            if hasattr(block, "text"):
                raw = block.text.strip()
                break

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlanError(f"Opus returned invalid JSON: {e}")

        try:
            return ExecutionPlan(**data)
        except Exception as e:
            raise PlanError(f"Plan schema validation failed: {e}")

    async def _validate(
        self, plan: ExecutionPlan, available_skills: list[str], settings
    ) -> None:
        # All skills must be available
        for step in plan.steps:
            if step.skill not in available_skills:
                raise PlanError(f"Step {step.step_id} references unavailable skill: {step.skill}")

        # No circular dependencies
        _topological_sort(plan.steps)

        # Token budget
        if plan.estimated_total_tokens > settings.sentinel_plan_token_budget:
            raise PlanError(
                f"Plan token estimate {plan.estimated_total_tokens} exceeds budget {settings.sentinel_plan_token_budget}"
            )

    async def _persist(self, plan: ExecutionPlan) -> None:
        try:
            from app.db.postgres import execute
            await execute(
                "INSERT INTO sentinel_execution_plans (goal_id, plan) VALUES ($1, $2::jsonb)",
                plan.goal_id, json.dumps(plan.model_dump()),
            )
        except Exception as e:
            logger.warning("Failed to persist plan: %s", e)

    async def _get_available_skills(self) -> list[str]:
        """Return list of all registered skill names from the brain's registry."""
        try:
            from app.brain.dispatcher import _build_skill_registry
            reg = _build_skill_registry()
            return list(reg._skills.keys())
        except Exception:
            return ["chat", "server_shell", "github_read", "github_write", "task_create",
                    "task_read", "sentry_read", "docker_drift", "cert_check", "patch_audit",
                    "dns_audit", "backup_check", "goal", "goal_status", "reflect", "wake"]
