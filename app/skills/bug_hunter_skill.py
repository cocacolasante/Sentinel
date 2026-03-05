"""
BugHunterSkill — on-demand trigger for the Autonomous Bug Hunter.

Kicks off the bug_hunt Celery task and returns immediately.
The full report is posted to #brain-alerts by the worker.
"""

from __future__ import annotations

import logging

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)


class BugHunterSkill(BaseSkill):
    name = "bug_hunt"
    description = (
        "Scan logs for bugs autonomously: fetches Loki error patterns, clusters them, "
        "runs LLM root-cause analysis, proposes fixes, posts report to #brain-alerts, "
        "and auto-creates fix tasks for high-severity actionable bugs."
    )
    trigger_intents = ["bug_hunt"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        hours = int(params.get("hours", 24))
        focus = params.get("focus", "")  # optional service or error type filter

        try:
            from app.worker.bug_hunter_tasks import run_bug_hunt

            run_bug_hunt.apply_async(
                kwargs={"hours": hours},
                queue="tasks_general",
            )
            dispatched = True
        except Exception as exc:
            logger.error("BugHunterSkill failed to dispatch task: %s", exc)
            dispatched = False

        if dispatched:
            context = (
                f"Bug hunt kicked off for the last *{hours} hours*. "
                f"{'Focus: ' + focus + '. ' if focus else ''}"
                "Sentinel is scanning Loki for error patterns, clustering them, "
                "and running LLM root-cause analysis on each cluster. "
                "The full report will be posted to *#brain-alerts* in about 30-60 seconds, "
                "including root causes, proposed fixes, and any auto-created fix tasks."
            )
        else:
            context = (
                "Failed to dispatch the bug hunt task — Celery worker may be unavailable. "
                "Check the worker logs."
            )

        return SkillResult(context_data=context, skill_name=self.name)
