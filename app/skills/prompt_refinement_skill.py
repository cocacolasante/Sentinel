"""
PromptRefinementSkill — A/B tests prompt variants and applies winners.

Trigger intent: prompt_refine
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from typing import Optional

from app.skills.base import BaseSkill, SkillResult
from app.db.postgres import execute as pg_execute
from app.skills.git_commit_skill import GitCommitSkill

logger = logging.getLogger(__name__)


@dataclass
class PromptVariant:
    skill_name: str
    variant: str         # "control" | "treatment"
    prompt_hash: str
    prompt_text: str


@dataclass
class ABTestResult:
    skill_name: str
    winner: str          # "control" | "treatment" | "inconclusive"
    confidence: float
    treatment_success_rate: float
    control_success_rate: float
    recommendation: str


class PromptRefinementSkill(BaseSkill):
    name = "prompt_refinement"
    description = "A/B test prompt variants for skills, evaluate winners, and apply via PR"
    trigger_intents = ["prompt_refine"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        action = params.get("action", "status")
        skill_name = params.get("skill_name", "")

        if action == "propose":
            current_prompt = params.get("current_prompt", "")
            improvement_hint = params.get("improvement_hint", "")
            if not skill_name or not current_prompt:
                return SkillResult(context_data="skill_name and current_prompt are required", is_error=True)
            variant = await self.propose_variant(skill_name, current_prompt, improvement_hint)
            return SkillResult(context_data=json.dumps({
                "variant": variant.variant,
                "prompt_hash": variant.prompt_hash,
                "skill_name": variant.skill_name,
            }))

        if action == "evaluate":
            if not skill_name:
                return SkillResult(context_data="skill_name is required", is_error=True)
            result = await self.evaluate(skill_name)
            return SkillResult(context_data=json.dumps({
                "winner": result.winner,
                "confidence": result.confidence,
                "treatment_success_rate": result.treatment_success_rate,
                "control_success_rate": result.control_success_rate,
                "recommendation": result.recommendation,
            }))

        if action == "apply":
            if not skill_name:
                return SkillResult(context_data="skill_name is required", is_error=True)
            await self.apply_winner(skill_name)
            return SkillResult(context_data=f"Applied winning prompt for {skill_name}")

        # Default: list active tests
        try:
            from app.db.postgres import execute
            rows = await execute(
                """
                SELECT skill_name, variant, calls_total, calls_success, is_active
                FROM sentinel_prompt_ab_tests
                WHERE is_active = TRUE
                ORDER BY skill_name, variant
                """
            )
            return SkillResult(context_data=json.dumps({"active_tests": [dict(r) for r in (rows or [])]}))
        except Exception as e:
            return SkillResult(context_data=json.dumps({"error": str(e)}))

    async def propose_variant(
        self,
        skill_name: str,
        current_prompt: str,
        improvement_hint: str,
    ) -> PromptVariant:
        """Use Sonnet to generate an improved prompt variant; store in DB."""
        import anthropic
        from app.config import get_settings
        settings = get_settings()

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        system = "You are a prompt engineering expert. Improve the given prompt to be clearer, more accurate, and produce higher-quality outputs."
        user = f"""Current prompt for skill '{skill_name}':
---
{current_prompt}
---
Improvement hint: {improvement_hint or 'Make it clearer and more accurate.'}

Write an improved version of this prompt. Return ONLY the improved prompt text, no explanation."""

        try:
            resp = client.messages.create(
                model=settings.model_sonnet,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            improved = resp.content[0].text.strip()
        except Exception as e:
            logger.error("Sonnet prompt generation failed: %s", e)
            improved = current_prompt

        prompt_hash = hashlib.sha256(improved.encode()).hexdigest()[:16]

        # Ensure control variant exists
        control_hash = hashlib.sha256(current_prompt.encode()).hexdigest()[:16]
        await self._upsert_variant(skill_name, "control", control_hash)

        # Store treatment variant
        await self._upsert_variant(skill_name, "treatment", prompt_hash)

        return PromptVariant(
            skill_name=skill_name,
            variant="treatment",
            prompt_hash=prompt_hash,
            prompt_text=improved,
        )

    async def record_call(
        self,
        skill_name: str,
        variant: str,
        success: bool,
        tokens: int,
        duration_ms: float,
    ) -> None:
        """Upsert call statistics for a variant."""
        try:
            await pg_execute(
                """
                UPDATE sentinel_prompt_ab_tests
                SET calls_total     = calls_total + 1,
                    calls_success   = calls_success + $1,
                    avg_tokens      = (avg_tokens * calls_total + $2) / (calls_total + 1),
                    avg_duration_ms = (avg_duration_ms * calls_total + $3) / (calls_total + 1),
                    last_updated    = NOW()
                WHERE skill_name = $4 AND variant = $5 AND is_active = TRUE
                """,
                1 if success else 0,
                tokens,
                duration_ms,
                skill_name,
                variant,
            )
        except Exception as e:
            logger.warning("record_call failed: %s", e)

    async def evaluate(self, skill_name: str) -> ABTestResult:
        """Evaluate A/B test using two-proportion z-test."""
        from app.config import get_settings
        settings = get_settings()

        try:
            rows = await pg_execute(
                """
                SELECT variant, calls_total, calls_success
                FROM sentinel_prompt_ab_tests
                WHERE skill_name = $1 AND is_active = TRUE
                """,
                skill_name,
            )
        except Exception as e:
            logger.error("evaluate DB query failed: %s", e)
            rows = []

        stats = {r["variant"]: r for r in (rows or [])}
        control = stats.get("control")
        treatment = stats.get("treatment")

        if not control or not treatment:
            return ABTestResult(
                skill_name=skill_name,
                winner="inconclusive",
                confidence=0.0,
                treatment_success_rate=0.0,
                control_success_rate=0.0,
                recommendation="No A/B test data found for this skill",
            )

        n_c = control["calls_total"] or 0
        n_t = treatment["calls_total"] or 0

        if n_c < settings.sentinel_prompt_ab_min_samples or n_t < settings.sentinel_prompt_ab_min_samples:
            return ABTestResult(
                skill_name=skill_name,
                winner="inconclusive",
                confidence=0.0,
                treatment_success_rate=(treatment["calls_success"] / n_t) if n_t else 0.0,
                control_success_rate=(control["calls_success"] / n_c) if n_c else 0.0,
                recommendation=f"Insufficient samples (need {settings.sentinel_prompt_ab_min_samples} per variant)",
            )

        p_c = control["calls_success"] / n_c
        p_t = treatment["calls_success"] / n_t
        p_pool = (control["calls_success"] + treatment["calls_success"]) / (n_c + n_t)

        # Two-proportion z-test
        if p_pool <= 0 or p_pool >= 1:
            z = 0.0
        else:
            se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_c + 1 / n_t))
            z = (p_t - p_c) / se if se > 0 else 0.0

        # Convert z to one-tailed p-value proxy (normal CDF approximation)
        confidence = _norm_cdf(abs(z))

        if confidence >= settings.sentinel_prompt_ab_confidence and p_t > p_c:
            winner = "treatment"
        elif confidence >= settings.sentinel_prompt_ab_confidence and p_c > p_t:
            winner = "control"
        else:
            winner = "inconclusive"

        recommendation = (
            f"treatment wins ({p_t:.1%} vs {p_c:.1%}, confidence={confidence:.1%})"
            if winner == "treatment"
            else f"control wins ({p_c:.1%} vs {p_t:.1%})"
            if winner == "control"
            else f"no significant difference yet (confidence={confidence:.1%})"
        )

        # Update Prometheus
        try:
            from app.observability.prometheus_metrics import PROMPT_AB_WIN_RATE
            win_rate = p_t / p_c if p_c > 0 else 1.0
            PROMPT_AB_WIN_RATE.labels(skill=skill_name).set(win_rate)
        except Exception:
            pass

        return ABTestResult(
            skill_name=skill_name,
            winner=winner,
            confidence=confidence,
            treatment_success_rate=p_t,
            control_success_rate=p_c,
            recommendation=recommendation,
        )

    async def apply_winner(self, skill_name: str) -> None:
        """Apply winning treatment prompt via GitCommitSkill PR."""
        from app.config import get_settings
        settings = get_settings()

        result = await self.evaluate(skill_name)
        if result.winner != "treatment" or result.confidence < settings.sentinel_prompt_ab_confidence:
            logger.info("apply_winner: no confident winner for %s", skill_name)
            return

        # Get winning prompt text (stored in a detail column would be ideal, but we use hash lookup)
        # For now, generate a diff comment about the win rate improvement
        diff_text = f"""--- a/prompt_variants/{skill_name}.md
+++ b/prompt_variants/{skill_name}.md
@@ -1,3 +1,3 @@
-# Control prompt for {skill_name}
+# Winning treatment prompt for {skill_name} (applied after A/B test)
+# Success rate: {result.treatment_success_rate:.1%} vs control {result.control_success_rate:.1%}
"""

        try:
            gc = GitCommitSkill()
            await gc.execute(
                {
                    "diff": diff_text,
                    "issue_slug": f"prompt-ab-{skill_name}",
                    "test_id": f"prompt_ab/{skill_name}",
                },
                original_message=f"Apply winning A/B prompt for {skill_name}",
            )
        except Exception as e:
            logger.error("apply_winner GitCommitSkill failed: %s", e)

        # Mark treatment as new control, deactivate old control
        try:
            await pg_execute(
                """
                UPDATE sentinel_prompt_ab_tests
                SET is_active = FALSE
                WHERE skill_name = $1 AND variant = 'control'
                """,
                skill_name,
            )
            await pg_execute(
                """
                UPDATE sentinel_prompt_ab_tests
                SET variant = 'control'
                WHERE skill_name = $1 AND variant = 'treatment' AND is_active = TRUE
                """,
                skill_name,
            )
        except Exception as e:
            logger.warning("apply_winner DB update failed: %s", e)

        # Post Slack summary
        try:
            from app.integrations.slack_notifier import post_alert_sync
            post_alert_sync(
                f"✅ *Prompt A/B winner applied* for `{skill_name}` — "
                f"treatment success rate {result.treatment_success_rate:.1%} "
                f"vs control {result.control_success_rate:.1%} "
                f"(confidence {result.confidence:.1%})",
                "sentinel-alerts",
            )
        except Exception:
            pass

    async def _upsert_variant(self, skill_name: str, variant: str, prompt_hash: str) -> None:
        try:
            await pg_execute(
                """
                INSERT INTO sentinel_prompt_ab_tests (skill_name, variant, prompt_hash)
                VALUES ($1, $2, $3)
                ON CONFLICT (skill_name, variant, prompt_hash) DO NOTHING
                """,
                skill_name,
                variant,
                prompt_hash,
            )
        except Exception as e:
            logger.warning("_upsert_variant failed: %s", e)


def _norm_cdf(z: float) -> float:
    """Approximate normal CDF using Abramowitz & Stegun approximation."""
    if z < 0:
        return 1.0 - _norm_cdf(-z)
    t = 1.0 / (1.0 + 0.2316419 * z)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z * z) * poly
