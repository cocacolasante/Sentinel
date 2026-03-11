"""
ProcessMonitor — watches the managed application process.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ProcessMonitor:
    def __init__(self, settings):
        self._settings = settings

    def is_running(self) -> bool:
        """Return True if the application process is running."""
        try:
            import psutil
            name = self._settings.app_process_name
            return any(
                p.name() == name or name in " ".join(p.cmdline())
                for p in psutil.process_iter(["name", "cmdline"])
            )
        except Exception:
            return False

    def get_pid(self) -> int | None:
        """Return the PID of the application process, or None."""
        try:
            import psutil
            name = self._settings.app_process_name
            for p in psutil.process_iter(["name", "cmdline", "pid"]):
                if p.name() == name or name in " ".join(p.cmdline()):
                    return p.pid
        except Exception:
            pass
        return None

    async def watch(self, relay, interval: int = 30) -> None:
        """Poll process status; send PROCESS_DOWN event if not running."""
        was_running = True
        while True:
            try:
                running = self.is_running()
                if was_running and not running:
                    logger.warning("Process %s is DOWN", self._settings.app_process_name)
                    await relay.send("PROCESS_DOWN", {
                        "process_name": self._settings.app_process_name,
                        "app_dir": self._settings.app_dir,
                    })
                elif not was_running and running:
                    logger.info("Process %s recovered", self._settings.app_process_name)
                    await relay.send("PROCESS_UP", {
                        "process_name": self._settings.app_process_name,
                    })
                was_running = running
            except Exception as exc:
                logger.error("Process monitor error: %s", exc)
            await asyncio.sleep(interval)
