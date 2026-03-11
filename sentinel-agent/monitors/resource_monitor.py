"""
ResourceMonitor — monitors CPU, memory, and disk usage.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ResourceMonitor:
    def __init__(self, settings):
        self._settings = settings

    async def watch(self, relay, interval: int = 60) -> None:
        """Poll resource usage every interval seconds; send RESOURCE_ALERT if threshold exceeded."""
        while True:
            try:
                import psutil
                cpu_pct = psutil.cpu_percent(interval=1)
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage(self._settings.app_dir)

                checks = [
                    ("cpu_pct", cpu_pct, self._settings.resource_cpu_threshold),
                    ("mem_pct", mem.percent, self._settings.resource_mem_threshold),
                    ("disk_pct", disk.percent, self._settings.resource_disk_threshold),
                ]
                for metric, value, threshold in checks:
                    if value >= threshold:
                        await relay.send("RESOURCE_ALERT", {
                            "metric": metric,
                            "value": round(value, 1),
                            "threshold": threshold,
                        })
                        logger.warning("Resource alert: %s=%.1f%% >= %.0f%%", metric, value, threshold)
            except Exception as exc:
                logger.error("Resource monitor error: %s", exc)
            await asyncio.sleep(interval)
