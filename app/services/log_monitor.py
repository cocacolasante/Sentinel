"""
Log monitor — polls the Loki "Errors & Warnings" stream.

Uses the same LogQL query as the Grafana "Errors & Warnings" panel:
  {job="docker"} |~ "(?i)(error|exception|traceback|critical|warning)"

Every 30 s it fetches new log lines from Loki, extracts the container name
from the Loki stream labels, and feeds each matching line into ErrorCollector
so remediation tasks can be auto-created.
"""

import asyncio
import time
from typing import Dict, Any
from loguru import logger

import httpx

from app.services.error_logger import error_collector

_LOKI_URL = "http://loki:3100"
_QUERY = '{job="docker"} |~ "(?i)(error|exception|traceback|critical|warning)"'
_POLL_INTERVAL = 30   # seconds between Loki polls
_LOOK_BACK = 60       # seconds to look back on the very first poll


def _classify_line(line: str) -> str:
    """Map a log line to a broad error_type label."""
    low = line.lower()
    if "traceback" in low or "exception" in low:
        return "exception"
    if "critical" in low:
        return "critical"
    if "warning" in low or "warn" in low:
        return "warning"
    return "error"


class LogMonitor:
    """Polls Loki for new error/warning log lines across all Sentinel containers."""

    def __init__(self):
        self._last_ts_ns: int = 0   # nanosecond timestamp of the newest line seen

    async def start_monitoring(self) -> None:
        """Run the polling loop indefinitely (call once as a background task)."""
        logger.info("LogMonitor: starting Loki-based error stream polling")
        # Seed last_ts so the first poll only looks back _LOOK_BACK seconds
        self._last_ts_ns = (int(time.time()) - _LOOK_BACK) * 1_000_000_000

        while True:
            try:
                await self._poll()
            except Exception as e:
                logger.error("LogMonitor poll error: {}", e)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll(self) -> None:
        """Fetch new error/warning lines from Loki since the last seen timestamp."""
        start_ns = self._last_ts_ns + 1          # exclusive lower bound
        end_ns   = int(time.time()) * 1_000_000_000

        params = {
            "query": _QUERY,
            "start": str(start_ns),
            "end":   str(end_ns),
            "limit": "500",
            "direction": "forward",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_LOKI_URL}/loki/api/v1/query_range", params=params)

        if resp.status_code != 200:
            logger.warning("Loki returned {}: {}", resp.status_code, resp.text[:200])
            return

        data = resp.json()
        results = data.get("data", {}).get("result", [])

        newest_ts = self._last_ts_ns
        count = 0

        for stream in results:
            labels: Dict[str, str] = stream.get("stream", {})
            # Derive a human-readable service name from available labels
            container = (
                labels.get("container_name")
                or labels.get("container_id")
                or labels.get("source")
                or labels.get("agent")
                or "unknown"
            )
            service = container.replace("ai-", "").replace("brain-", "")

            for ts_str, log_line in stream.get("values", []):
                ts_ns = int(ts_str)
                if ts_ns > newest_ts:
                    newest_ts = ts_ns

                error_type = _classify_line(log_line)
                await error_collector.log_error(
                    service=service,
                    error_type=error_type,
                    message=log_line[:300],
                    context={"loki_labels": labels, "ts_ns": ts_ns},
                )
                count += 1

        if count:
            logger.debug("LogMonitor: ingested {} line(s) from Loki", count)

        self._last_ts_ns = newest_ts

    async def get_service_health(self) -> Dict[str, Dict[str, Any]]:
        """Return error counts per service from the in-memory buffer."""
        services: Dict[str, int] = {}
        for entry in error_collector.error_buffer:
            svc = entry["service"]
            services[svc] = services.get(svc, 0) + 1

        return {
            svc: {"error_count": cnt, "source": "loki"}
            for svc, cnt in services.items()
        }


log_monitor = LogMonitor()
