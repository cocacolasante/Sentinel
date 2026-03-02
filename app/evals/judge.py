"""
Haiku-based LLM judge for eval scoring.

Uses Claude Haiku to score AI responses against defined criteria.
Costs fractions of a cent per eval — cheap enough to run weekly on all tests.

Returns a structured dict: {score, passed, reasoning}
"""

from __future__ import annotations

import json
import logging

import anthropic

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

_JUDGE_SYSTEM = (
    "You are an impartial AI evaluator. Your job is to score AI assistant responses "
    "against specific quality criteria. Be strict and accurate. "
    "Return ONLY valid JSON — no explanation outside the JSON object."
)

_JUDGE_TEMPLATE = """{judge_prompt}

Criteria to evaluate against:
{criteria_list}

AI Response to evaluate:
---
{response}
---

Return ONLY this JSON (no markdown, no preamble):
{{
  "score": <integer 0-10>,
  "passed": <true if score >= {threshold}, else false>,
  "reasoning": "<2-3 sentences explaining the score and which criteria passed or failed>"
}}"""


def judge_response(
    response: str,
    criteria: list[str],
    judge_prompt: str,
    threshold: int,
) -> dict:
    """
    Score an AI response against criteria using Haiku as judge.

    Returns:
        {"score": int, "passed": bool, "reasoning": str}
        Falls back to {"score": 0, "passed": False, "reasoning": "<error>"} on failure.
    """
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    criteria_list = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(criteria))
    prompt = _JUDGE_TEMPLATE.format(
        judge_prompt=judge_prompt,
        criteria_list=criteria_list,
        response=response[:6000],   # guard against very long responses
        threshold=threshold,
    )

    try:
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = result.content[0].text.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)
        score = max(0, min(10, int(data.get("score", 0))))
        return {
            "score":     score,
            "passed":    score >= threshold,
            "reasoning": str(data.get("reasoning", "")),
        }

    except json.JSONDecodeError as exc:
        logger.warning("Judge returned non-JSON: %s", exc)
        return {"score": 0, "passed": False, "reasoning": f"Judge parse error: {exc}"}
    except Exception as exc:
        logger.error("Judge call failed: %s", exc)
        return {"score": 0, "passed": False, "reasoning": f"Judge error: {exc}"}
