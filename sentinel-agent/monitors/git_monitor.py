"""
GitMonitor — watches for git SHA changes in the managed application.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class GitMonitor:
    def __init__(self, settings):
        self._settings = settings
        self._last_sha: str | None = None

    async def poll(self, relay, interval: int = 300) -> None:
        """Poll git HEAD every interval seconds; send GIT_UPDATE on change."""
        while True:
            try:
                sha = await self._get_sha()
                if sha and sha != self._last_sha:
                    if self._last_sha is not None:
                        await relay.send("GIT_UPDATE", {
                            "sha": sha,
                            "previous_sha": self._last_sha,
                        })
                        logger.info("Git update: %s → %s", self._last_sha, sha)
                    self._last_sha = sha
            except Exception as exc:
                logger.warning("Git monitor error: %s", exc)
            await asyncio.sleep(interval)

    async def _get_sha(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", self._settings.app_dir, "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return stdout.decode().strip() if stdout else None
        except Exception:
            return None
