"""
Log monitor — polls the Loki "Errors & Warnings" stream once per hour.

Uses the same LogQL query as the Grafana "Errors & Warnings" panel:
  {job="docker"} |~ "(?i)(error|exception|traceback|critical|warning)"

Each poll:
  1. Fetches all new lines since the last run
  2. Buckets them by (service, error_type)
  3. Sorts buckets by frequency — most occurring first
  4. Submits at most _MAX_TASKS_PER_POLL buckets to ErrorCollector
  5. ErrorCollector debounces (1 hour) and creates approval-gated tasks
"""

import asyncio
import json
import time
from collections import defaultdict
from typing import Dict, Any, List, Tuple
from loguru import logger

import httpx

from app.services.error_logger import error_collector

_LOKI_URL = "http://loki:3100"
_QUERY = '{job="docker"} |~ "(?i)(error|exception|traceback|critical|warning)"'
_POLL_INTERVAL = 3600  # 1 hour between polls
_LOOK_BACK = 3600  # look back 1 hour on the very first poll
_MAX_TASKS_PER_POLL = 5  # max new tasks created per poll cycle


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
    """Polls Loki hourly, buckets errors by type, surfaces the most frequent first."""

    def __init__(self):
        self._last_ts_ns: int = 0

    async def start_monitoring(self) -> None:
        """Run the polling loop indefinitely (launched as a background task)."""
        logger.info("LogMonitor: starting hourly Loki error poll")
        self._last_ts_ns = (int(time.time()) - _LOOK_BACK) * 1_000_000_000

        while True:
            try:
                await self._poll()
            except Exception as e:
                logger.error("LogMonitor poll error: {}", e)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll(self) -> None:
        """Fetch new lines from Loki, bucket by (service, error_type), submit top N."""
        start_ns = self._last_ts_ns + 1
        end_ns = int(time.time()) * 1_000_000_000

        params = {
            "query": _QUERY,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": "2000",
            "direction": "forward",
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{_LOKI_URL}/loki/api/v1/query_range", params=params)
            if resp.status_code != 200:
                logger.warning("Loki returned {}: {}", resp.status_code, resp.text[:200])
                return
            data = resp.json()
        except httpx.RequestError as e:
            logger.error("Loki request failed: {}", e)
            return
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Loki response: {}", e)
            return

        results = data.get("data", {}).get("result", [])
        newest_ts = self._last_ts_ns

        # bucket[(service, error_type)] = list of log lines
        buckets: Dict[Tuple[str, str], List[str]] = defaultdict(list)

        for stream in results:
            labels: Dict[str, str] = stream.get("stream", {})
            container = (
                labels.get("container_name")
                or labels.get("container_id")
                or labels.get("source")
                or labels.get("agent")
                or "unknown"
            )
            service = container.replace("ai-", "").replace("brain-", "")

            for ts_str, log_line in stream.get("values", []):
                try:
                    ts_ns = int(ts_str)
                except (ValueError, TypeError):
                    continue
                if ts_ns > newest_ts:
                    newest_ts = ts_ns
                error_type = _classify_line(log_line)
                buckets[(service, error_type)].append(log_line)

        self._last_ts_ns = newest_ts

        if not buckets:
            return

        # Sort by frequency descending — most occurring errors first
        ranked: List[Tuple[Tuple[str, str], List[str]]] = sorted(
            buckets.items(), key=lambda kv: len(kv[1]), reverse=True
        )

        total_lines = sum(len(v) for v in buckets.values())
        logger.info(
            "LogMonitor: {} line(s) -> {} bucket(s), submitting top {}",
            total_lines,
            len(ranked),
            _MAX_TASKS_PER_POLL,
        )

        submitted = 0
        for (service, error_type), lines in ranked:
            if submitted >= _MAX_TASKS_PER_POLL:
                break
            sample = lines[0][:300]
            summary = f"{len(lines)} occurrence(s) in the last hour.\nSample: {sample}"
            created = await error_collector.log_error(
                service=service,
                error_type=error_type,
                message=summary,
                context={"count": len(lines), "sample": sample},
            )
            if created:
                submitted += 1

        if submitted:
            logger.info("LogMonitor: {} task(s) queued for approval", submitted)

    async def get_service_health(self) -> Dict[str, Dict[str, Any]]:
        """Return error counts per service from the in-memory buffer."""
        services: Dict[str, int] = {}
        for entry in error_collector.error_buffer:
            svc = entry["service"]
            services[svc] = services.get(svc, 0) + 1
        return {svc: {"error_count": cnt, "source": "loki"} for svc, cnt in services.items()}


log_monitor = LogMonitor()
