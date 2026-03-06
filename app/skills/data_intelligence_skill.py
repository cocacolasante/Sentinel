"""
Data Intelligence Skill

Analyzes data across Sentinel's systems:
  - Time series trends (slope, direction, rate of change)
  - Anomaly detection (z-score statistical outliers)
  - Pattern discovery (hour-of-day, day-of-week recurring peaks)
  - Multi-source correlation (Prometheus + PostgreSQL + Sentry)

Trigger examples:
  "analyze API usage trends"
  "detect anomalies in server metrics"
  "show memory usage patterns over the last 7 days"
  "are there traffic spikes every Tuesday?"
  "what's causing the spike at 2pm?"
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Optional

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)


# ── Pure-Python statistical helpers ───────────────────────────────────────────


def _stats(values: list[float]) -> dict:
    """Compute descriptive statistics for a list of floats."""
    if not values:
        return {}
    n = len(values)
    mu = statistics.mean(values)
    std = statistics.stdev(values) if n > 1 else 0.0
    sorted_v = sorted(values)
    p95 = sorted_v[int(n * 0.95)] if n >= 20 else sorted_v[-1]
    p05 = sorted_v[int(n * 0.05)] if n >= 20 else sorted_v[0]

    # Linear trend: slope via least-squares over index
    if n >= 3:
        xs = list(range(n))
        x_mean = (n - 1) / 2
        num = sum((xs[i] - x_mean) * (values[i] - mu) for i in range(n))
        den = sum((xs[i] - x_mean) ** 2 for i in range(n))
        slope = num / den if den else 0.0
        # Normalise slope as % change per period relative to mean
        trend_pct = (slope * n / mu * 100) if mu != 0 else 0.0
    else:
        slope = 0.0
        trend_pct = 0.0

    trend_dir = (
        "rising" if trend_pct > 5 else "falling" if trend_pct < -5 else "stable"
    )

    return {
        "n": n,
        "mean": round(mu, 3),
        "std": round(std, 3),
        "min": round(sorted_v[0], 3),
        "max": round(sorted_v[-1], 3),
        "p05": round(p05, 3),
        "p95": round(p95, 3),
        "trend_pct": round(trend_pct, 1),
        "trend_dir": trend_dir,
        "cv": round(std / mu * 100, 1) if mu != 0 else 0.0,  # coefficient of variation
    }


def _detect_anomalies(
    points: list[tuple[float, float]],
    threshold: float = 2.5,
    window: int = 30,
) -> list[dict]:
    """
    Rolling z-score anomaly detection.
    Uses a sliding window of `window` samples to compute local mean + std,
    then flags points where |z| > threshold.
    Returns list of {timestamp, value, z_score, direction}.
    """
    if len(points) < window + 1:
        # Fall back to global z-score
        values = [v for _, v in points]
        if len(values) < 3:
            return []
        mu = statistics.mean(values)
        std = statistics.stdev(values) if len(values) > 1 else 0.0
        if std == 0:
            return []
        anomalies = []
        for ts, val in points:
            z = (val - mu) / std
            if abs(z) >= threshold:
                anomalies.append({
                    "timestamp": ts,
                    "value": round(val, 3),
                    "z_score": round(z, 2),
                    "direction": "high" if z > 0 else "low",
                    "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    ),
                })
        return anomalies

    anomalies = []
    for i in range(window, len(points)):
        window_vals = [v for _, v in points[i - window : i]]
        mu = statistics.mean(window_vals)
        std = statistics.stdev(window_vals) if len(window_vals) > 1 else 0.0
        if std == 0:
            continue
        ts, val = points[i]
        z = (val - mu) / std
        if abs(z) >= threshold:
            anomalies.append({
                "timestamp": ts,
                "value": round(val, 3),
                "z_score": round(z, 2),
                "direction": "high" if z > 0 else "low",
                "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                ),
            })
    return anomalies


def _detect_patterns(
    points: list[tuple[float, float]],
) -> dict:
    """
    Discover recurring time patterns:
      - Hourly averages (which hour of day is typically highest/lowest)
      - Day-of-week averages (which day is busiest)
    Returns structured pattern data.
    """
    if len(points) < 24:
        return {"insufficient_data": True}

    # Group by hour-of-day (UTC)
    hourly: dict[int, list[float]] = {h: [] for h in range(24)}
    daily: dict[int, list[float]] = {d: [] for d in range(7)}  # 0=Mon

    for ts, val in points:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        hourly[dt.hour].append(val)
        daily[dt.weekday()].append(val)

    hourly_avg = {
        h: round(statistics.mean(vs), 3) for h, vs in hourly.items() if vs
    }
    daily_avg = {
        d: round(statistics.mean(vs), 3) for d, vs in daily.items() if vs
    }

    _DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    peak_hour = max(hourly_avg, key=hourly_avg.get) if hourly_avg else None
    trough_hour = min(hourly_avg, key=hourly_avg.get) if hourly_avg else None
    peak_day = max(daily_avg, key=daily_avg.get) if daily_avg else None
    trough_day = min(daily_avg, key=daily_avg.get) if daily_avg else None

    # Detect "business hours" vs "off hours" contrast
    biz_hours = [hourly_avg.get(h, 0) for h in range(9, 18)]
    off_hours = [hourly_avg.get(h, 0) for h in list(range(0, 9)) + list(range(18, 24))]
    biz_mean = round(statistics.mean(biz_hours), 3) if biz_hours else 0
    off_mean = round(statistics.mean(off_hours), 3) if off_hours else 0

    return {
        "hourly_avg": hourly_avg,
        "daily_avg": {_DAYS[d]: v for d, v in daily_avg.items()},
        "peak_hour": peak_hour,
        "peak_hour_label": f"{peak_hour:02d}:00 UTC" if peak_hour is not None else "N/A",
        "trough_hour": trough_hour,
        "trough_hour_label": f"{trough_hour:02d}:00 UTC" if trough_hour is not None else "N/A",
        "peak_day": _DAYS[peak_day] if peak_day is not None else "N/A",
        "trough_day": _DAYS[trough_day] if trough_day is not None else "N/A",
        "business_hours_mean": biz_mean,
        "off_hours_mean": off_mean,
        "biz_vs_off_ratio": round(biz_mean / off_mean, 2) if off_mean else None,
    }


def _format_report(
    metric_name: str,
    unit: str,
    window: str,
    points: list[tuple[float, float]],
    stats: dict,
    anomalies: list[dict],
    patterns: dict,
    source: str,
) -> str:
    """Render analysis findings as structured text for the LLM to interpret."""
    lines = [
        f"## Data Intelligence Report",
        f"**Metric:** {metric_name}  |  **Window:** {window}  |  **Source:** {source}",
        f"**Samples:** {stats.get('n', 0)}  |  **Unit:** {unit}",
        "",
    ]

    # Trend summary
    if stats:
        lines += [
            "### Summary Statistics",
            f"- Mean: {stats['mean']} {unit}",
            f"- Std dev: {stats['std']} {unit} (CV: {stats['cv']}%)",
            f"- Range: {stats['min']} – {stats['max']} {unit}",
            f"- P05 / P95: {stats['p05']} / {stats['p95']} {unit}",
            f"- Trend: **{stats['trend_dir']}** ({stats['trend_pct']:+.1f}% over window)",
            "",
        ]

    # Anomalies
    lines.append("### Anomalies Detected")
    if anomalies:
        lines.append(f"Found **{len(anomalies)} anomalies** (z-score threshold ≥ 2.5):")
        for a in anomalies[:10]:  # cap at 10 for readability
            dir_label = "spike" if a["direction"] == "high" else "drop"
            lines.append(
                f"- {a['datetime']}: {a['value']} {unit} "
                f"({dir_label}, z={a['z_score']})"
            )
        if len(anomalies) > 10:
            lines.append(f"  ... and {len(anomalies) - 10} more")
    else:
        lines.append("No significant anomalies detected (all values within 2.5σ of rolling mean).")
    lines.append("")

    # Patterns
    lines.append("### Time Patterns")
    if patterns.get("insufficient_data"):
        lines.append("Insufficient data for pattern analysis (need ≥24 samples).")
    else:
        lines += [
            f"- Peak hour of day: **{patterns['peak_hour_label']}** "
            f"(avg {patterns['hourly_avg'].get(patterns['peak_hour'], 0)} {unit})",
            f"- Lowest hour: **{patterns['trough_hour_label']}** "
            f"(avg {patterns['hourly_avg'].get(patterns['trough_hour'], 0)} {unit})",
            f"- Busiest day of week: **{patterns['peak_day']}** "
            f"(avg {patterns['daily_avg'].get(patterns['peak_day'], 0)} {unit})",
            f"- Quietest day: **{patterns['trough_day']}**",
        ]
        ratio = patterns.get("biz_vs_off_ratio")
        if ratio is not None:
            direction = "higher" if ratio > 1 else "lower"
            lines.append(
                f"- Business hours (09–18 UTC) vs off-hours: "
                f"{ratio}x {direction} during business hours"
            )

    # Hourly heatmap (sparkline style)
    if not patterns.get("insufficient_data") and patterns.get("hourly_avg"):
        ha = patterns["hourly_avg"]
        max_v = max(ha.values()) if ha else 1
        if max_v > 0:
            bar_chars = " ▁▂▃▄▅▆▇█"
            spark = ""
            for h in range(24):
                v = ha.get(h, 0)
                idx = min(8, int(v / max_v * 8))
                spark += bar_chars[idx]
            lines.append(f"\nHourly activity (00h→23h): `{spark}`")

    return "\n".join(lines)


# ── Data gathering helpers ─────────────────────────────────────────────────────


async def _gather_prometheus(metric: str, window: str) -> tuple[list[tuple[float, float]], str, str]:
    """
    Fetch metric from Prometheus.
    Returns (points, unit_label, source_label).
    """
    from app.integrations.prometheus_client import PrometheusClient, METRIC_QUERIES

    client = PrometheusClient()
    promql = METRIC_QUERIES.get(metric, metric)
    points = await client.get_metric(metric, window=window)

    unit_map = {
        "cpu": "%", "memory": "%", "disk_read": "B/s", "disk_write": "B/s",
        "network_in": "B/s", "network_out": "B/s",
        "redis_ops": "ops/s", "redis_clients": "clients", "redis_memory_mb": "MB",
        "pg_connections": "connections", "pg_db_size_mb": "MB",
        "celery_tasks": "tasks", "celery_active": "tasks", "celery_failures": "tasks",
    }
    unit = unit_map.get(metric, "")
    source = f"Prometheus ({promql[:60]}{'…' if len(promql) > 60 else ''})"
    return points, unit, source


async def _gather_db_tasks(window: str) -> tuple[list[tuple[float, float]], str, str]:
    """
    Task creation rate from Postgres — tasks created per hour bucket.
    Returns (points, unit, source).
    """
    from app.integrations.prometheus_client import _WINDOW_SECONDS
    import time as _time

    secs = _WINDOW_SECONDS.get(window, 86400)
    try:
        from app.db import postgres

        rows = postgres.execute(
            """
            SELECT
                date_trunc('hour', created_at) AS bucket,
                COUNT(*) AS cnt
            FROM tasks
            WHERE created_at >= NOW() - INTERVAL '%s seconds'
            GROUP BY bucket
            ORDER BY bucket
            """,
            (secs,),
        )
        points = []
        for r in rows or []:
            bucket = r["bucket"]
            ts = bucket.timestamp() if hasattr(bucket, "timestamp") else float(str(bucket))
            points.append((ts, float(r["cnt"])))
        return points, "tasks/hr", "PostgreSQL (tasks table)"
    except Exception as exc:
        logger.warning("DB tasks gather failed: %s", exc)
        return [], "tasks/hr", "PostgreSQL (tasks table)"


async def _gather_db_milestones(window: str) -> tuple[list[tuple[float, float]], str, str]:
    """AI action milestones per hour from Postgres."""
    from app.integrations.prometheus_client import _WINDOW_SECONDS

    secs = _WINDOW_SECONDS.get(window, 86400)
    try:
        from app.db import postgres

        rows = postgres.execute(
            """
            SELECT
                date_trunc('hour', triggered_at) AS bucket,
                COUNT(*) AS cnt
            FROM ai_milestones
            WHERE triggered_at >= NOW() - INTERVAL '%s seconds'
            GROUP BY bucket
            ORDER BY bucket
            """,
            (secs,),
        )
        points = []
        for r in rows or []:
            bucket = r["bucket"]
            ts = bucket.timestamp() if hasattr(bucket, "timestamp") else float(str(bucket))
            points.append((ts, float(r["cnt"])))
        return points, "actions/hr", "PostgreSQL (ai_milestones table)"
    except Exception as exc:
        logger.warning("DB milestones gather failed: %s", exc)
        return [], "actions/hr", "PostgreSQL (ai_milestones table)"


async def _gather_sentry_errors(window: str) -> tuple[list[tuple[float, float]], str, str]:
    """Sentry error event frequency bucketed into hourly counts (approximated from issue data)."""
    try:
        from app.integrations.sentry_client import SentryClient

        client = SentryClient()
        if not client.is_configured():
            return [], "errors/hr", "Sentry"

        issues = await client.list_issues(query="is:unresolved", limit=50)
        # Sentry gives us `firstSeen`/`lastSeen` and `count` per issue.
        # Build a rough time series from the `count` data by spreading evenly.
        # This is approximate but useful for pattern detection.
        points_map: dict[int, float] = {}
        now = datetime.now(tz=timezone.utc)
        for issue in issues or []:
            try:
                count = int(issue.get("count", 0))
                last = issue.get("lastSeen", "")
                if last:
                    dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    hour_ts = int(dt.timestamp() // 3600) * 3600
                    points_map[hour_ts] = points_map.get(hour_ts, 0) + count
            except Exception:
                pass

        points = sorted((float(ts), v) for ts, v in points_map.items())
        return points, "errors/hr", "Sentry (issue last-seen distribution)"
    except Exception as exc:
        logger.warning("Sentry errors gather failed: %s", exc)
        return [], "errors/hr", "Sentry"


# ── Metric → source routing ────────────────────────────────────────────────────

_PROMETHEUS_METRICS = {
    "cpu", "memory", "disk_read", "disk_write",
    "network_in", "network_out", "redis_ops", "redis_clients",
    "redis_memory_mb", "pg_connections", "pg_db_size_mb",
    "celery_tasks", "celery_active", "celery_failures",
}

_METRIC_ALIASES: dict[str, str] = {
    "api_usage": "tasks",
    "api": "tasks",
    "requests": "tasks",
    "traffic": "tasks",
    "activity": "milestones",
    "actions": "milestones",
    "errors": "sentry_errors",
    "error_rate": "sentry_errors",
    "server": "cpu",
    "server_metrics": "cpu",
    "system": "cpu",
    "ram": "memory",
    "heap": "memory",
    "disk": "disk_read",
    "network": "network_in",
    "redis": "redis_ops",
    "celery": "celery_tasks",
    "workers": "celery_active",
    "postgres": "pg_connections",
    "db": "pg_connections",
}


async def _gather_data(
    metric: str, window: str
) -> tuple[list[tuple[float, float]], str, str]:
    """Route metric name to appropriate data source."""
    metric = _METRIC_ALIASES.get(metric.lower(), metric.lower())

    if metric in _PROMETHEUS_METRICS:
        points, unit, source = await _gather_prometheus(metric, window)
        if points:
            return points, unit, source
        # Fall back to DB if Prometheus returns nothing
        logger.info("Prometheus returned no data for %s, falling back to DB", metric)

    if metric == "tasks":
        return await _gather_db_tasks(window)
    if metric == "milestones":
        return await _gather_db_milestones(window)
    if metric == "sentry_errors":
        return await _gather_sentry_errors(window)

    # Unknown metric — try DB tasks as default
    return await _gather_db_tasks(window)


# ── Multi-metric analysis ─────────────────────────────────────────────────────


async def _run_full_analysis(
    metric: str,
    window: str,
    threshold: float,
) -> str:
    """Gather data, run all analyses, return formatted report."""
    display_name = metric.replace("_", " ").title()

    points, unit, source = await _gather_data(metric, window)

    if not points:
        return (
            f"[data_intelligence: No data available for metric '{metric}' "
            f"over window '{window}'. "
            "Prometheus may be unavailable or the metric returned no results. "
            "Available DB metrics: tasks, milestones. "
            "Available Prometheus metrics: cpu, memory, redis_ops, celery_tasks, etc.]"
        )

    values = [v for _, v in points]
    st = _stats(values)
    anomalies = _detect_anomalies(points, threshold=threshold)
    patterns = _detect_patterns(points)

    return _format_report(
        metric_name=display_name,
        unit=unit,
        window=window,
        points=points,
        stats=st,
        anomalies=anomalies,
        patterns=patterns,
        source=source,
    )


async def _run_multi_metric_overview(window: str) -> str:
    """Parallel gather + summary for cpu, memory, tasks, errors."""
    targets = [
        ("cpu", "CPU %"),
        ("memory", "Memory %"),
        ("tasks", "Task activity"),
        ("celery_failures", "Celery failures"),
    ]

    tasks = [_gather_data(m, window) for m, _ in targets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    lines = [
        f"## System Overview — Last {window}",
        "",
    ]
    for (metric, label), result in zip(targets, results):
        if isinstance(result, Exception) or not result or not result[0]:
            lines.append(f"**{label}**: no data")
            continue
        pts, unit, _ = result
        vals = [v for _, v in pts]
        if not vals:
            lines.append(f"**{label}**: no data")
            continue
        st = _stats(vals)
        anomalies = _detect_anomalies(pts)
        anomaly_note = f" — ⚠️ {len(anomalies)} anomalies" if anomalies else ""
        lines.append(
            f"**{label}**: avg {st['mean']} {unit}, "
            f"max {st['max']} {unit}, trend {st['trend_dir']} ({st['trend_pct']:+.1f}%)"
            f"{anomaly_note}"
        )

    lines += [
        "",
        "_Ask me to drill into any metric: cpu, memory, tasks, errors, redis, celery, etc._",
    ]
    return "\n".join(lines)


# ── Skill ─────────────────────────────────────────────────────────────────────


class DataIntelligenceSkill(BaseSkill):
    name = "data_intelligence"
    description = (
        "Analyze data across Sentinel's systems. Performs time series trend analysis, "
        "statistical anomaly detection (z-score), and recurring pattern discovery "
        "(peak hours, day-of-week patterns). Data sources: Prometheus (CPU, memory, Redis, "
        "Celery, Postgres metrics), PostgreSQL (task activity, AI milestones), Sentry (errors). "
        "Use this for: 'analyze API usage', 'detect anomalies in server metrics', "
        "'show traffic patterns', 'what's causing the Tuesday spike'."
    )
    trigger_intents = ["data_intelligence"]
    approval_category = ApprovalCategory.NONE  # read-only

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "analyze")
        metric = params.get("metric", "auto")
        window = params.get("window", "24h")
        threshold = float(params.get("threshold", 2.5))
        promql = params.get("promql", "")

        # Custom PromQL expression
        if promql:
            from app.integrations.prometheus_client import PrometheusClient

            client = PrometheusClient()
            points = await client.query_range(promql, window=window)
            if not points:
                return SkillResult(
                    context_data=f"[data_intelligence: Custom PromQL returned no data: {promql}]",
                    skill_name=self.name,
                )
            values = [v for _, v in points]
            st = _stats(values)
            anomalies = _detect_anomalies(points, threshold=threshold)
            patterns = _detect_patterns(points)
            report = _format_report(
                metric_name=f"Custom ({promql[:40]}…)",
                unit="",
                window=window,
                points=points,
                stats=st,
                anomalies=anomalies,
                patterns=patterns,
                source=f"Prometheus ({promql[:60]})",
            )
            return SkillResult(context_data=report, skill_name=self.name)

        # Overview: no specific metric requested
        if metric in ("auto", "all", "overview", ""):
            report = await _run_multi_metric_overview(window)
            return SkillResult(context_data=report, skill_name=self.name)

        # Specific metric analysis
        report = await _run_full_analysis(metric, window, threshold)
        return SkillResult(context_data=report, skill_name=self.name)
