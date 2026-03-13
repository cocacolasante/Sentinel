"""
DeployVerifierSkill — after a deploy, poll the health endpoint and trigger
automatic rollback if health checks fail.

Intent: verify_deploy
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from app.config import get_settings
from app.integrations.slack_notifier import post_alert_sync
from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)
settings = get_settings()

_HEALTH_ENDPOINT = "/api/v1/health"
_TASKS_ENDPOINT = "/api/v1/tasks"
_MAX_POLLS = 12
_POLL_INTERVAL = 10  # seconds


class DeployVerifierSkill(BaseSkill):
    name = "deploy_verifier"
    description = (
        "After a deploy, poll the health endpoint up to 12 times (2 min total) and verify "
        "key routes respond correctly. Triggers automatic rollback via docker-compose if checks fail. "
        "Use for: 'verify the deploy', 'check deployment health', 'confirm deploy worked'."
    )
    trigger_intents = ["verify_deploy"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        base_url = params.get("base_url", f"https://{settings.domain}")
        base_url = base_url.rstrip("/")

        # Poll health endpoint
        health_ok = False
        last_status = 0

        async with aiohttp.ClientSession() as session:
            for attempt in range(1, _MAX_POLLS + 1):
                try:
                    async with session.get(
                        f"{base_url}{_HEALTH_ENDPOINT}",
                        timeout=aiohttp.ClientTimeout(total=8),
                        ssl=False,
                    ) as resp:
                        last_status = resp.status
                        if resp.status == 200:
                            health_ok = True
                            break
                except Exception as exc:
                    logger.debug("Health poll %d/%d failed: %s", attempt, _MAX_POLLS, exc)

                if attempt < _MAX_POLLS:
                    await asyncio.sleep(_POLL_INTERVAL)

        if not health_ok:
            # Trigger rollback
            rollback_msg = await self._trigger_rollback()
            summary = f"❌ Deploy FAILED — health check returned {last_status} after {_MAX_POLLS} polls. {rollback_msg}"
            try:
                post_alert_sync(summary)
            except Exception:
                pass
            return SkillResult(
                context_data=json.dumps({"ok": False, "summary": summary}),
                is_error=True,
            )

        # Verify tasks endpoint
        tasks_ok = False
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    f"{base_url}{_TASKS_ENDPOINT}",
                    timeout=aiohttp.ClientTimeout(total=8),
                    ssl=False,
                ) as resp:
                    tasks_ok = resp.status == 200
            except Exception as exc:
                logger.debug("Tasks endpoint check failed: %s", exc)

        status_emoji = "✅" if tasks_ok else "⚠️"
        summary = (
            f"{status_emoji} Deploy verified: `{base_url}` — "
            f"health: {'OK' if health_ok else 'FAIL'}, "
            f"tasks endpoint: {'OK' if tasks_ok else 'FAIL'}"
        )

        try:
            post_alert_sync(summary)
        except Exception:
            pass

        return SkillResult(
            context_data=json.dumps({"ok": True, "health_ok": health_ok, "tasks_ok": tasks_ok, "summary": summary})
        )

    async def _trigger_rollback(self) -> str:
        """Attempt to roll back via docker-compose restart."""
        try:
            from app.utils.shell import run as shell_run

            result = await shell_run(
                ["docker", "compose", "restart", "brain"],
                cwd="/root/sentinel-workspace",
                timeout=120,
            )
            if result.ok:
                return "Rollback (restart) triggered successfully."
            else:
                return f"Rollback attempt failed: {result.stderr[:200]}"
        except Exception as exc:
            logger.error("Rollback error: %s", exc)
            return f"Rollback error: {exc}"
