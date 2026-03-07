"""
Skill Discovery — gap detection and auto-build proposal.

When the Brain is asked to do something it doesn't have a skill for,
or when intent confidence is very low, this meta-skill:

  1. Lists all registered skills with descriptions
  2. Asks Claude to determine if any existing skill covers the request
  3. If no skill covers it, proposes building a new skill via repo_write
  4. Returns a rich context block with the analysis + action plan

Intent: skill_discover
"""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

_DISCOVERY_SYSTEM = """You are a skill gap analyzer for Sentinel, an AI assistant platform.
You are analyzing Sentinel's own skill registry. The codebase lives at /root/sentinel-workspace on GitHub at cocacolasante/Sentinel.
Do NOT ask where the repo is — all skills are written directly to app/skills/{skill_name}.py in this workspace.

Given a user request and a list of available skills, determine:
1. Which existing skill (if any) best handles this request (may be partial match)
2. Whether a new skill needs to be built
3. If a new skill is needed, provide a concrete implementation plan

Return ONLY valid JSON:
{
  "best_existing_skill": "<skill_name or null>",
  "coverage": "full | partial | none",
  "gap_description": "What capability is missing",
  "new_skill_needed": true | false,
  "proposed_skill": {
    "name": "skill_name",
    "intent": "intent_name",
    "description": "one-line description",
    "integration": "what API/service it needs",
    "implementation_hints": "brief notes for implementation"
  } | null
}"""


class SkillDiscoverySkill(BaseSkill):
    name = "skill_discover"
    description = (
        "Detect when no skill exists for a task — analyze the gap, find the closest "
        "existing skill, or propose building a new one"
    )
    trigger_intents = ["skill_discover"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        # Lazy import to avoid circular deps
        from app.brain.dispatcher import _build_skill_registry
        from app.config import get_settings
        import anthropic

        settings = get_settings()

        # Build skill list description
        reg = _build_skill_registry()
        skills = reg.list_all_descriptions()

        # Ask Claude Haiku to analyze the gap
        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system=_DISCOVERY_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": (f"User request: {original_message}\n\nAvailable skills:\n{skills}"),
                    }
                ],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            analysis = json.loads(raw)
        except Exception as exc:
            analysis = {
                "best_existing_skill": None,
                "coverage": "none",
                "gap_description": f"Analysis failed: {exc}",
                "new_skill_needed": True,
                "proposed_skill": None,
            }

        coverage = analysis.get("coverage", "none")
        existing = analysis.get("best_existing_skill")
        proposed = analysis.get("proposed_skill")

        # Build human-readable context
        lines = []
        if coverage == "full" and existing:
            lines.append(
                f"The existing **{existing}** skill should handle this request. "
                "Try rephrasing your request using that skill's intent directly."
            )
        elif coverage == "partial" and existing:
            lines.append(f"The **{existing}** skill partially covers this, but: {analysis.get('gap_description', '')}")
        else:
            lines.append(
                f"**No existing skill covers this request.**\n"
                f"Gap: {analysis.get('gap_description', 'Unknown capability needed')}"
            )

        build_task_id: int | None = None
        originating_task_id: int | None = params.get("originating_task_id")

        if analysis.get("new_skill_needed") and proposed:
            skill_name = proposed.get("name", "new_skill")
            skill_intent = proposed.get("intent", skill_name)
            skill_desc = proposed.get("description", "")
            integration = proposed.get("integration", "")
            hints = proposed.get("implementation_hints", "")

            # Auto-build: create a workspace task routed to the LLM agent loop.
            # commands=[] routes to plan_and_execute which uses repo_write to write
            # proper code rather than fragile shell heredocs.
            try:
                from app.skills.task_skill import TaskCreateSkill

                _tc = TaskCreateSkill()
                _tc_params = {
                    "title": f"Build {skill_name} skill",
                    "description": (
                        f"Build a new Sentinel skill: {skill_desc}\n"
                        f"Skill name: {skill_name} | Intent: {skill_intent}\n"
                        f"Integration needed: {integration}\n"
                        f"Implementation notes: {hints}\n\n"
                        f"Working directory: /root/sentinel-workspace\n"
                        f"File to create: app/skills/{skill_name}.py\n"
                        f"Follow the BaseSkill pattern from app/skills/base.py.\n"
                        f"After writing the file, commit and push, then open a PR."
                    ),
                    "priority": 5,
                    "approval_level": 1,
                    "commands": [],
                    "execution_queue": "tasks_workspace",
                    "source": "skill_discovery",
                    "session_id": params.get("session_id", ""),
                }
                _tc_result = await _tc.execute(_tc_params, original_message)
                # Extract task ID from context
                import re as _re

                _m = _re.search(r"Task ID: #(\d+)", _tc_result.context_data or "")
                if _m:
                    build_task_id = int(_m.group(1))
            except Exception as _build_exc:
                lines.append(f"\n⚠️ Auto-build task creation failed: {_build_exc}")

            if build_task_id:
                # If there's an originating task, block it on the build task
                if originating_task_id:
                    try:
                        from app.db import postgres

                        postgres.execute(
                            "UPDATE tasks SET blocked_by=%s::jsonb WHERE id=%s",
                            (json.dumps([build_task_id]), originating_task_id),
                        )
                    except Exception:
                        pass

                # DM owner
                try:
                    from app.config import get_settings as _gs
                    from app.integrations.slack_notifier import post_dm

                    _s = _gs()
                    if _s.slack_owner_user_id:
                        _dm = f"🔧 *Missing skill detected: `{skill_name}`*\nAuto-building as Task #{build_task_id}."
                        if originating_task_id:
                            _dm += f"\nYour original Task #{originating_task_id} is blocked until it deploys."
                        await post_dm(_dm)
                except Exception:
                    pass

                lines.append(
                    f"\n**Auto-building new skill:** `{skill_name}` as Task #{build_task_id}\n"
                    f"Intent: `{skill_intent}` | Needs: {integration}\n"
                    f"Notes: {hints}"
                )
                if originating_task_id:
                    lines.append(f"\nTask #{originating_task_id} is now blocked until the skill deploys.")
            else:
                lines.append(
                    f"\n**Proposed new skill:** `{skill_name}`\n"
                    f"Intent: `{skill_intent}`\n"
                    f"Description: {skill_desc}\n"
                    f"Needs: {integration}\n"
                    f"Notes: {hints}\n\n"
                    "Would you like me to build this skill? Say **'yes, build it'** and I'll "
                    "write the code, add it to the dispatcher, and deploy it."
                )

        context = "\n".join(lines)
        return SkillResult(context_data=context, skill_name=self.name)


def _snake_to_camel(name: str) -> str:
    """Convert snake_case to CamelCase for class names."""
    return "".join(word.capitalize() for word in name.split("_"))


class SkillGapHandler:
    """
    Not a skill itself — a helper called by the dispatcher when confidence is low
    or when intent is 'chat' but the message looks action-oriented.
    """

    _ACTION_KEYWORDS = {
        "create",
        "build",
        "make",
        "set up",
        "deploy",
        "send",
        "post",
        "update",
        "delete",
        "remove",
        "get",
        "fetch",
        "list",
        "show",
        "connect",
        "integrate",
        "configure",
        "install",
        "run",
        "execute",
        "automate",
        "schedule",
        "monitor",
        "track",
        "notify",
        "alert",
        # Code / improvement actions
        "improve",
        "fix",
        "refactor",
        "optimize",
        "enhance",
        "rewrite",
        "add",
        "edit",
        "patch",
        "change",
        "implement",
        "write",
        "modify",
        "debug",
        "review",
        "analyse",
        "analyze",
    }

    @classmethod
    def should_trigger(cls, intent: str, confidence: float, message: str) -> bool:
        """Return True if we should run skill discovery instead of normal chat."""
        if intent == "chat":
            words = set(message.lower().split())
            action_hits = words & cls._ACTION_KEYWORDS
            # Very low confidence with any action word → trigger
            if confidence < 0.4 and action_hits:
                return True
            # Medium confidence with 2+ action words → genuine novel request
            if confidence < 0.55 and len(action_hits) >= 2:
                return True
        return False
