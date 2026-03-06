"""
Prometheus HTTP API client.

Queries the local Prometheus instance for metric data used by the
Data Intelligence skill (trends, anomaly detection, pattern discovery).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://prometheus:9090"
_TIMEOUT = 15.0

# ── Named metric shortcuts ────────────────────────────────────────────────────
# Map friendly names → PromQL expressions
METRIC_QUERIES: dict[str, str] = {
    # System
    "cpu": (
        "100 - (avg(irate(node_cpu_seconds_total{mode='idle'}[5m])) * 100)"
    ),
    "memory": (
        "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"
    ),
    "disk_read": "sum(irate(node_disk_read_bytes_total[5m]))",
    "disk_write": "sum(irate(node_disk_written_bytes_total[5m]))",
    "network_in": "sum(irate(node_network_receive_bytes_total{device!='lo'}[5m]))",
    "network_out": "sum(irate(node_network_transmit_bytes_total{device!='lo'}[5m]))",
    # Redis
    "redis_ops": "redis_instantaneous_ops_per_sec",
    "redis_clients": "redis_connected_clients",
    "redis_memory_mb": "redis_memory_used_bytes / 1024 / 1024",
    # Postgres
    "pg_connections": "pg_stat_activity_count",
    "pg_db_size_mb": "pg_database_size_bytes{datname='aibrain'} / 1024 / 1024",
    # Celery
    "celery_tasks": "sum(celery_tasks_total)",
    "celery_active": "sum(celery_active_tasks)",
    "celery_failures": "sum(celery_tasks_total{state='FAILURE'})",
}

# Window string → seconds
_WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
    "2d": 172800,
    "7d": 604800,
    "30d": 2592000,
}


class PrometheusClient:
    """Thin async wrapper around the Prometheus HTTP API."""

    def __init__(self, base_url: str = _DEFAULT_URL):
        self.base_url = base_url.rstrip("/")

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/-/ready", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    async def query(self, promql: str, at: Optional[float] = None) -> list[dict]:
        """Instant query. Returns list of {metric: labels, value: [ts, val]}."""
        params = {"query": promql}
        if at:
            params["time"] = str(at)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(f"{self.base_url}/api/v1/query", params=params)
                data = resp.json()
            if data.get("status") != "success":
                logger.warning("Prometheus query error: %s", data.get("error"))
                return []
            return data["data"]["result"]
        except Exception as exc:
            logger.warning("Prometheus query failed: %s", exc)
            return []

    async def query_range(
        self,
        promql: str,
        window: str = "24h",
        step: Optional[str] = None,
    ) -> list[tuple[float, float]]:
        """
        Range query over the last `window` period.
        Returns list of (unix_timestamp, float_value) sorted oldest-first.
        Auto-selects a sensible step size.
        """
        secs = _WINDOW_SECONDS.get(window, 86400)
        end = time.time()
        start = end - secs

        # Auto step: aim for ~120 data points
        if step is None:
            step_secs = max(60, secs // 120)
            step = f"{step_secs}s"

        params = {
            "query": promql,
            "start": str(start),
            "end": str(end),
            "step": step,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/query_range", params=params
                )
                data = resp.json()
            if data.get("status") != "success":
                logger.warning("Prometheus range query error: %s", data.get("error"))
                return []
            results = data["data"]["result"]
            if not results:
                return []
            # Sum across series if multiple (e.g. per-CPU)
            points: dict[float, float] = {}
            for series in results:
                for ts, val in series["values"]:
                    try:
                        points[float(ts)] = points.get(float(ts), 0.0) + float(val)
                    except (ValueError, TypeError):
                        pass
            return sorted(points.items())
        except Exception as exc:
            logger.warning("Prometheus range query failed: %s", exc)
            return []

    async def get_metric(
        self, metric_name: str, window: str = "24h"
    ) -> list[tuple[float, float]]:
        """
        Fetch a named metric (from METRIC_QUERIES) or raw PromQL over `window`.
        Returns (timestamp, value) pairs sorted oldest-first.
        """
        promql = METRIC_QUERIES.get(metric_name, metric_name)
        return await self.query_range(promql, window=window)
