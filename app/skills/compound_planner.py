"""
CompoundPlannerSkill — decompose multi-step requests into ordered task DAGs.

When Sentinel receives a compound request like "audit all servers AND fix issues AND deploy",
this skill decomposes it into concrete tasks with proper dependency chains (blocked_by).

Intent: compound_plan (never classified by IntentClassifier; reached only via dispatcher intercept)
"""

from __future__ import annotations

import json
import logging
import re

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)

_DECOMPOSE_SYSTEM = """\
You are a task planner for Sentinel. Decompose the multi-step request into concrete tasks.
Return ONLY valid JSON:
{{
  "plan_title": "...",
  "tasks": [
    {{"title":"...","description":"...","skill_hint":"<skill_name>","commands":[],"priority":3}}
  ],
  "dependencies": [[1,2],[2,3]]
}}

Rules:
- dependencies is a list of [A, B] pairs (1-based indices) meaning task A must complete before task B
- commands should be empty [] — tasks are executed by the LLM agent loop
- priority is 1 (low) to 5 (urgent)
- skill_hint should be one of the available skills or empty string if unsure
- Keep tasks concrete and actionable

Available skills: {skills_csv}
"""


class CompoundPlannerSkill(BaseSkill):
    name = "compound_planner"
    description = (
        "Decompose compound multi-step requests into an ordered task DAG. "
        "Used internally when Sentinel detects a multi-step orchestration request."
    )
    trigger_intents = ["compound_plan"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.brain.dispatcher import _build_skill_registry
        from app.config import get_settings
        import anthropic

        settings = get_settings()

        # Build available skills list
        reg = _build_skill_registry()
        skills_csv = ", ".join(
            s.name for s in reg.list_available() if s.name not in ("compound_planner", "chat")
        )

        # Ask Sonnet to decompose the request
        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model=settings.model_sonnet,
                max_tokens=4096,
                system=_DECOMPOSE_SYSTEM.format(skills_csv=skills_csv),
                messages=[
                    {
                        "role": "user",
                        "content": f"Decompose this multi-step request into tasks:\n\n{original_message}",
                    }
                ],
            )
            raw = resp.content[0].text.strip()
            # Strip markdown fence if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            plan = json.loads(raw)
        except Exception as exc:
            logger.error("CompoundPlannerSkill: LLM decomposition failed: %s", exc)
            return SkillResult(
                is_error=True,
                context_data=f"Could not decompose plan: {exc}",
                skill_name=self.name,
            )

        tasks_spec = plan.get("tasks") or []
        dependencies = plan.get("dependencies") or []
        plan_title = plan.get("plan_title", "Multi-step plan")

        if not tasks_spec:
            return SkillResult(
                is_error=True,
                context_data="Decomposition returned no tasks.",
                skill_name=self.name,
            )

        # Build blocked_by map (1-based index → list of 1-based blocker indices)
        blocked_by_map: dict[int, list[int]] = {}
        for dep in dependencies:
            if len(dep) == 2:
                a, b = dep[0], dep[1]  # a must complete before b
                blocked_by_map.setdefault(b, []).append(a)

        # Create tasks and collect real IDs
        from app.skills.task_skill import TaskCreateSkill
        from app.db import postgres

        session_id = params.get("session_id", "")
        task_ids: dict[int, int] = {}  # 1-based index → real DB id

        for idx, task_spec in enumerate(tasks_spec, start=1):
            tc = TaskCreateSkill()
            tc_params = {
                "title": task_spec.get("title", f"Step {idx}"),
                "description": task_spec.get("description", ""),
                "priority": task_spec.get("priority", 3),
                "approval_level": 1,
                "commands": task_spec.get("commands") or [],
                "execution_queue": "tasks_workspace",
                "source": "compound_planner",
                "session_id": session_id,
            }
            tc_result = await tc.execute(tc_params, original_message)
            m = re.search(r"Task ID: #(\d+)", tc_result.context_data or "")
            if m:
                task_ids[idx] = int(m.group(1))
            else:
                logger.warning("CompoundPlannerSkill: could not extract task ID from: %s", tc_result.context_data)

        # Apply blocked_by using real IDs
        for task_idx, blocker_indices in blocked_by_map.items():
            real_id = task_ids.get(task_idx)
            if not real_id:
                continue
            real_blocker_ids = [task_ids[bi] for bi in blocker_indices if bi in task_ids]
            if not real_blocker_ids:
                continue
            try:
                postgres.execute(
                    "UPDATE tasks SET blocked_by=%s::jsonb WHERE id=%s",
                    (json.dumps(real_blocker_ids), real_id),
                )
            except Exception as exc:
                logger.warning("CompoundPlannerSkill: could not set blocked_by on task #%s: %s", real_id, exc)

        # Build summary
        task_lines = []
        for idx, spec in enumerate(tasks_spec, start=1):
            real_id = task_ids.get(idx)
            blocker_real = [task_ids[bi] for bi in blocked_by_map.get(idx, []) if bi in task_ids]
            dep_str = f" (blocked by #{', #'.join(str(b) for b in blocker_real)})" if blocker_real else ""
            id_str = f" → Task #{real_id}" if real_id else ""
            task_lines.append(f"  {idx}. {spec.get('title','?')}{id_str}{dep_str}")

        summary = (
            f"**{plan_title}**\n\n"
            f"Created {len(task_ids)} tasks:\n"
            + "\n".join(task_lines)
        )

        return SkillResult(context_data=summary, skill_name=self.name)
