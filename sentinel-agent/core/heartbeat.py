"""
HeartbeatLoop — sends periodic HEARTBEAT messages to Brain.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class HeartbeatLoop:
    def __init__(self, relay, settings):
        self._relay = relay
        self._settings = settings

    async def run(self) -> None:
        """Send a HEARTBEAT every heartbeat_interval seconds."""
        while True:
            try:
                payload = await self._collect()
                await self._relay.send("HEARTBEAT", payload)
                logger.debug("Heartbeat sent: cpu=%(cpu_pct)s mem=%(mem_pct)s", payload)
            except Exception as exc:
                logger.warning("Heartbeat error: %s", exc)
            await asyncio.sleep(self._settings.heartbeat_interval)

    async def _collect(self) -> dict:
        """Collect system and application metrics."""
        import psutil

        cpu_pct = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(self._settings.app_dir)

        git_sha = await self._get_git_sha()
        http_status, http_latency_ms = await self._check_http()
        process_up = await self._check_process()

        return {
            "process_up": process_up,
            "cpu_pct": round(cpu_pct, 1),
            "mem_pct": round(mem.percent, 1),
            "disk_pct": round(disk.percent, 1),
            "git_sha": git_sha,
            "http_status": http_status,
            "http_latency_ms": http_latency_ms,
        }

    async def _get_git_sha(self) -> str | None:
        try:
            result = await asyncio.create_subprocess_exec(
                "git", "-C", self._settings.app_dir, "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=5)
            return stdout.decode().strip()[:12] if stdout else None
        except Exception:
            return None

    async def _check_http(self) -> tuple[int | None, float | None]:
        if not self._settings.app_health_url:
            return None, None
        try:
            import httpx
            import time
            start = time.monotonic()
            async with httpx.AsyncClient() as client:
                resp = await asyncio.wait_for(
                    client.get(self._settings.app_health_url),
                    timeout=10,
                )
            latency = (time.monotonic() - start) * 1000
            return resp.status_code, round(latency, 1)
        except Exception:
            return None, None

    async def _check_process(self) -> bool:
        try:
            import psutil
            name = self._settings.app_process_name
            return any(
                p.name() == name or name in " ".join(p.cmdline())
                for p in psutil.process_iter(["name", "cmdline"])
            )
        except Exception:
            return False
