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

_DISCOVERY_SYSTEM = """You are a skill gap analyzer for an AI assistant.
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
        reg    = _build_skill_registry()
        skills = reg.list_all_descriptions()

        # Ask Claude Haiku to analyze the gap
        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp   = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system=_DISCOVERY_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": (
                        f"User request: {original_message}\n\n"
                        f"Available skills:\n{skills}"
                    ),
                }],
            )
            raw    = resp.content[0].text.strip()
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
            lines.append(
                f"The **{existing}** skill partially covers this, but: "
                f"{analysis.get('gap_description', '')}"
            )
        else:
            lines.append(
                f"**No existing skill covers this request.**\n"
                f"Gap: {analysis.get('gap_description', 'Unknown capability needed')}"
            )

        if analysis.get("new_skill_needed") and proposed:
            lines.append(
                f"\n**Proposed new skill:** `{proposed.get('name')}`\n"
                f"Intent: `{proposed.get('intent')}`\n"
                f"Description: {proposed.get('description')}\n"
                f"Needs: {proposed.get('integration')}\n"
                f"Notes: {proposed.get('implementation_hints')}\n\n"
                "Would you like me to build this skill? Say **'yes, build it'** and I'll "
                "write the code, add it to the dispatcher, and deploy it."
            )

        context = "\n".join(lines)
        return SkillResult(context_data=context, skill_name=self.name)


class SkillGapHandler:
    """
    Not a skill itself — a helper called by the dispatcher when confidence is low
    or when intent is 'chat' but the message looks action-oriented.
    """

    _ACTION_KEYWORDS = {
        "create", "build", "make", "set up", "deploy", "send", "post",
        "update", "delete", "remove", "get", "fetch", "list", "show",
        "connect", "integrate", "configure", "install", "run", "execute",
        "automate", "schedule", "monitor", "track", "notify", "alert",
        # Code / improvement actions
        "improve", "fix", "refactor", "optimize", "enhance", "rewrite",
        "add", "edit", "patch", "change", "implement", "write", "modify",
        "debug", "review", "analyse", "analyze",
    }

    @classmethod
    def should_trigger(cls, intent: str, confidence: float, message: str) -> bool:
        """Return True if we should run skill discovery instead of normal chat."""
        if confidence < 0.4 and intent == "chat":
            words = set(message.lower().split())
            if words & cls._ACTION_KEYWORDS:
                return True
        return False
