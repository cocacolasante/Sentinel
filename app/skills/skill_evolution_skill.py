"""
SkillEvolutionSkill — writes a new skill from a prose description using AST analysis + Sonnet.

Trigger intent: skill_evolve

Only active when sentinel_skill_evolution_enabled=True.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)


@dataclass
class EvolvedSkill:
    skill_name: str
    file_path: str
    trigger_intents: list[str]
    description: str
    test_file_path: str
    pr_url: Optional[str]


class SkillEvolutionSkill(BaseSkill):
    name = "skill_evolution"
    description = "Write a new skill file from a prose description (requires SENTINEL_SKILL_EVOLUTION_ENABLED=true)"
    trigger_intents = ["skill_evolve"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        from app.config import get_settings
        settings = get_settings()

        if not settings.sentinel_skill_evolution_enabled:
            return SkillResult(
                context_data="skill_evolution is disabled — set SENTINEL_SKILL_EVOLUTION_ENABLED=true to enable",
                is_error=True,
            )

        title = params.get("title", original_message)
        detail = params.get("detail", "")
        if not title:
            return SkillResult(context_data="title/description required", is_error=True)

        from app.skills.reflection_skill import ReflectionProposal
        proposal = ReflectionProposal(
            title=title,
            description=detail or title,
            priority=5.0,
            auto_actionable=False,
            type="new_skill",
            detail=detail or title,
        )

        evolved = await self.evolve(proposal)
        return SkillResult(context_data=json.dumps({
            "skill_name": evolved.skill_name,
            "file_path": evolved.file_path,
            "trigger_intents": evolved.trigger_intents,
            "test_file_path": evolved.test_file_path,
            "pr_url": evolved.pr_url,
        }))

    async def evolve(self, proposal) -> EvolvedSkill:
        """Full evolution pipeline: search → generate → validate → test → PR."""
        from app.config import get_settings
        settings = get_settings()

        if not settings.sentinel_skill_evolution_enabled:
            raise RuntimeError("skill_evolution is disabled")

        # 1. Search for similar skill patterns
        similar_context = await self._search_similar_skills(proposal.title)

        # 2. Generate skill with Sonnet
        skill_code, skill_name, trigger_intents = await self._generate_skill(
            proposal=proposal,
            similar_context=similar_context,
            settings=settings,
        )

        file_path = f"app/skills/{skill_name}_skill.py"
        test_file_path = f"tests/test_{skill_name}_skill.py"

        # 3. Sandbox validate
        valid = await self._validate(skill_code)
        if not valid:
            raise RuntimeError(f"Generated skill for '{proposal.title}' failed sandbox validation")

        # 4. Generate tests
        test_code = await self._generate_tests(skill_name, skill_code, settings)

        # 5. Commit via GitCommitSkill
        diff = self._build_diff(file_path, skill_code, test_file_path, test_code)
        pr_url = await self._commit_pr(skill_name, diff)

        # 6. Post Slack summary
        try:
            from app.integrations.slack_notifier import post_alert_sync
            post_alert_sync(
                f"🧬 *New skill evolved*: `{skill_name}` — intents: {trigger_intents}\n"
                f"PR: {pr_url or '(no PR)'}",
                "sentinel-alerts",
            )
        except Exception:
            pass

        # 7. Update counter
        try:
            from app.observability.prometheus_metrics import SKILLS_EVOLVED_TOTAL
            SKILLS_EVOLVED_TOTAL.inc()
        except Exception:
            pass

        return EvolvedSkill(
            skill_name=skill_name,
            file_path=file_path,
            trigger_intents=trigger_intents,
            description=proposal.title,
            test_file_path=test_file_path,
            pr_url=pr_url,
        )

    async def _search_similar_skills(self, title: str) -> str:
        """Try CodeIndexSkill for similar patterns; return empty string on failure."""
        try:
            from app.skills.code_index_skill import CodeIndexSkill
            cis = CodeIndexSkill()
            result = await cis.execute({"action": "search", "query": title, "limit": 3}, "")
            return result.context_data or ""
        except Exception as e:
            logger.debug("_search_similar_skills: %s", e)
            return ""

    async def _generate_skill(self, proposal, similar_context: str, settings) -> tuple[str, str, list[str]]:
        import anthropic
        import re

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        similar_block = f"\n\nSimilar existing skill patterns for reference:\n{similar_context}" if similar_context else ""

        prompt = f"""You are a senior Python engineer writing a new Sentinel AI skill.

Task description: {proposal.title}
Detail: {getattr(proposal, 'detail', proposal.title)}
{similar_block}

Write a complete, production-ready Sentinel skill Python file following this structure:
1. Module docstring with skill purpose
2. Imports (use `from app.skills.base import BaseSkill, SkillResult`)
3. A single class inheriting BaseSkill with:
   - `name: str` class attribute (snake_case)
   - `description: str` class attribute
   - `trigger_intents: list[str]` class attribute (1-3 relevant intents)
   - `async def execute(self, params: dict, original_message: str = "") -> SkillResult:`
4. Return SkillResult with context_data (JSON string) or is_error=True on failure
5. Use `from app.config import get_settings` for settings
6. Use `from app.db.postgres import execute` for DB
7. Use `from app.integrations.slack_notifier import post_alert_sync` for Slack

Return ONLY valid Python code. No markdown fences. No explanations."""

        resp = client.messages.create(
            model=settings.model_sonnet,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        code = resp.content[0].text.strip()
        if code.startswith("```"):
            code = code.split("```")[1]
            if code.startswith("python"):
                code = code[6:]
            code = code.rstrip("`").strip()

        # Extract class name → skill_name
        match = re.search(r"class\s+(\w+)\s*\(BaseSkill\)", code)
        class_name = match.group(1) if match else "GeneratedSkill"
        # Convert CamelCase to snake_case for file name
        skill_name = re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower().replace("_skill", "")

        # Extract trigger_intents
        intents_match = re.search(r'trigger_intents\s*=\s*\[([^\]]+)\]', code)
        if intents_match:
            raw = intents_match.group(1)
            trigger_intents = [s.strip().strip('"\'') for s in raw.split(",") if s.strip()]
        else:
            trigger_intents = [skill_name.replace("_", " ").split()[0]]

        return code, skill_name, trigger_intents

    async def _validate(self, code: str) -> bool:
        """Sandbox validate the generated code."""
        try:
            from app.skills.sandbox_validator_skill import SandboxValidatorSkill
            sv = SandboxValidatorSkill()
            result = await sv.execute({"code": code, "language": "python"}, "")
            return not result.is_error
        except Exception as e:
            logger.warning("_validate via SandboxValidatorSkill failed: %s — attempting ast.parse", e)
            try:
                import ast
                ast.parse(code)
                return True
            except SyntaxError:
                return False

    async def _generate_tests(self, skill_name: str, skill_code: str, settings) -> str:
        """Use TestGeneratorSkill or Sonnet to write tests."""
        try:
            from app.skills.test_generator_skill import TestGeneratorSkill
            tg = TestGeneratorSkill()
            result = await tg.execute(
                {"source_code": skill_code, "skill_name": skill_name},
                "",
            )
            if result.context_data and not result.is_error:
                return result.context_data
        except Exception as e:
            logger.debug("TestGeneratorSkill failed: %s — falling back to Sonnet", e)

        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=settings.model_sonnet,
            max_tokens=1500,
            messages=[{"role": "user", "content": f"Write pytest unit tests for this Python skill:\n\n{skill_code}\n\nReturn ONLY valid Python test code."}],
        )
        test_code = resp.content[0].text.strip()
        if test_code.startswith("```"):
            test_code = test_code.split("```")[1]
            if test_code.startswith("python"):
                test_code = test_code[6:]
            test_code = test_code.rstrip("`").strip()
        return test_code

    def _build_diff(self, file_path: str, skill_code: str, test_path: str, test_code: str) -> str:
        """Build a unified diff for skill + test files."""
        skill_lines = skill_code.splitlines()
        test_lines = test_code.splitlines()

        diff_parts = [
            f"--- /dev/null",
            f"+++ b/{file_path}",
            f"@@ -0,0 +1,{len(skill_lines)} @@",
        ] + [f"+{line}" for line in skill_lines]

        diff_parts += [
            f"--- /dev/null",
            f"+++ b/{test_path}",
            f"@@ -0,0 +1,{len(test_lines)} @@",
        ] + [f"+{line}" for line in test_lines]

        return "\n".join(diff_parts)

    async def _commit_pr(self, skill_name: str, diff: str) -> Optional[str]:
        """Commit skill files on a new branch and open a PR."""
        try:
            from app.skills.git_commit_skill import GitCommitSkill
            gc = GitCommitSkill()
            result = await gc.execute(
                {
                    "diff": diff,
                    "issue_slug": f"evolved-skill-{skill_name}",
                    "test_id": f"skill_evolution/{skill_name}",
                },
                original_message=f"Evolve new skill: {skill_name}",
            )
            ctx = result.context_data or ""
            # Extract PR URL from result
            import re
            match = re.search(r"https://github\.com/\S+/pull/\d+", ctx)
            return match.group(0) if match else None
        except Exception as e:
            logger.error("_commit_pr failed: %s", e)
            return None
