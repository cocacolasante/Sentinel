"""
HttpMonitor — periodic HTTP health check of the managed application.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class HttpMonitor:
    def __init__(self, settings):
        self._settings = settings

    async def poll(self, relay, interval: int = 60) -> None:
        """Poll the health URL every interval seconds; send HTTP_STATUS event."""
        if not self._settings.app_health_url:
            return

        while True:
            try:
                import httpx
                start = time.monotonic()
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(self._settings.app_health_url)
                latency_ms = round((time.monotonic() - start) * 1000, 1)
                await relay.send("HTTP_STATUS", {
                    "status_code": resp.status_code,
                    "latency_ms": latency_ms,
                    "url": self._settings.app_health_url,
                })
            except Exception as exc:
                logger.warning("HTTP health check failed: %s", exc)
                await relay.send("HTTP_STATUS", {
                    "status_code": None,
                    "latency_ms": None,
                    "url": self._settings.app_health_url,
                    "error": str(exc),
                })
            await asyncio.sleep(interval)
