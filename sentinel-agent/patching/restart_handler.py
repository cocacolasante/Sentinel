"""
RestartHandler — restart the managed application after patching.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class RestartHandler:
    def __init__(self, settings, process_monitor):
        self._settings = settings
        self._process_monitor = process_monitor

    async def restart(self, relay, patch_id: str) -> bool:
        """
        Execute app_restart_cmd and poll for process recovery.
        Returns True if process is up within 60s.
        """
        cmd = self._settings.app_restart_cmd
        if not cmd:
            logger.warning("No app_restart_cmd configured — skipping restart")
            return True

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                logger.error("Restart command timed out")
                return False

            # Poll for process up
            for _ in range(30):  # 60s total
                await asyncio.sleep(2)
                if self._process_monitor.is_running():
                    logger.info("Process recovered after restart")
                    return True

            logger.error("Process did not recover after restart")
            return False

        except Exception as exc:
            logger.error("Restart failed: %s", exc)
            return False
