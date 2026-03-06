"""
Unit tests for data_intelligence_skill pure math functions.

No external dependencies — tests only the stat / anomaly / pattern / format
functions that are pure Python.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app.skills.data_intelligence_skill import (
    _detect_anomalies,
    _detect_patterns,
    _format_report,
    _stats,
    DataIntelligenceSkill,
)


# ── _stats ────────────────────────────────────────────────────────────────────


def test_stats_empty_list_returns_empty():
    assert _stats([]) == {}


def test_stats_single_value():
    r = _stats([42.0])
    assert r["n"] == 1
    assert r["mean"] == 42.0
    assert r["std"] == 0.0
    assert r["min"] == 42.0
    assert r["max"] == 42.0


def test_stats_two_values():
    r = _stats([10.0, 20.0])
    assert r["n"] == 2
    assert r["mean"] == 15.0
    assert r["min"] == 10.0
    assert r["max"] == 20.0
    assert r["std"] > 0


def test_stats_returns_all_keys():
    r = _stats([1.0, 2.0, 3.0, 4.0, 5.0])
    for key in ("n", "mean", "std", "min", "max", "p05", "p95", "trend_pct", "trend_dir", "cv"):
        assert key in r, f"Missing key: {key}"


def test_stats_stable_trend():
    """Constant values should have a stable trend."""
    r = _stats([10.0] * 20)
    assert r["trend_dir"] == "stable"
    assert r["trend_pct"] == 0.0


def test_stats_rising_trend():
    """Monotonically increasing values should have a rising trend."""
    r = _stats([float(i) for i in range(1, 50)])
    assert r["trend_dir"] == "rising"


def test_stats_falling_trend():
    """Monotonically decreasing values should have a falling trend."""
    r = _stats([float(50 - i) for i in range(50)])
    assert r["trend_dir"] == "falling"


def test_stats_p95_for_large_set():
    """p95 should be near the high end of the distribution."""
    vals = [float(i) for i in range(100)]
    r = _stats(vals)
    assert r["p95"] >= 90.0


def test_stats_cv_zero_for_constant():
    """Coefficient of variation should be 0 for constant input."""
    r = _stats([5.0, 5.0, 5.0, 5.0, 5.0])
    assert r["cv"] == 0.0


def test_stats_mean_zero_cv_handled():
    """CV computation should not raise when mean == 0."""
    r = _stats([0.0, 0.0, 0.0])
    assert r["cv"] == 0.0


def test_stats_two_elements_no_trend():
    """Fewer than 3 elements means no trend slope calculation."""
    r = _stats([1.0, 2.0])
    assert r["trend_pct"] == 0.0
    assert r["trend_dir"] == "stable"


# ── _detect_anomalies ────────────────────────────────────────────────────────


def _ts_series(values: list[float], start: float | None = None) -> list[tuple[float, float]]:
    """Build a (timestamp, value) series starting at `start` with 60s spacing."""
    base = start or time.time() - len(values) * 60
    return [(base + i * 60, v) for i, v in enumerate(values)]


def test_detect_anomalies_empty():
    assert _detect_anomalies([]) == []


def test_detect_anomalies_too_few_points():
    pts = _ts_series([1.0, 2.0])
    assert _detect_anomalies(pts) == []


def test_detect_anomalies_no_spike():
    pts = _ts_series([10.0] * 50)
    # All values equal → std=0 in every rolling window → nothing flagged
    result = _detect_anomalies(pts)
    assert result == []


def test_detect_anomalies_detects_spike():
    """Spike at index 35 is detectable because the preceding 30-point window has variance."""
    # Use a slightly oscillating baseline so the rolling window has non-zero std
    vals = [10.0 + (i % 3) * 0.5 for i in range(50)]
    vals[35] = 999.0  # extreme outlier well past the window size (30)
    pts = _ts_series(vals)
    result = _detect_anomalies(pts, threshold=2.0)
    assert len(result) >= 1
    assert result[0]["direction"] == "high"


def test_detect_anomalies_detects_drop():
    """Drop at index 35 is detectable because the preceding window has variance."""
    vals = [100.0 + (i % 3) * 0.5 for i in range(50)]
    vals[35] = 0.001  # extreme drop well past the window size (30)
    pts = _ts_series(vals)
    result = _detect_anomalies(pts, threshold=2.0)
    low_anomalies = [a for a in result if a["direction"] == "low"]
    assert len(low_anomalies) >= 1


def test_detect_anomalies_returns_required_fields():
    vals = [10.0 + (i % 3) * 0.5 for i in range(50)]
    vals[35] = 500.0
    pts = _ts_series(vals)
    result = _detect_anomalies(pts, threshold=2.0)
    if result:
        a = result[0]
        for key in ("timestamp", "value", "z_score", "direction", "datetime"):
            assert key in a, f"Missing key: {key}"


def test_detect_anomalies_global_fallback():
    """Short series uses global z-score fallback."""
    pts = _ts_series([10.0, 10.0, 10.0, 10.0, 999.0], start=0.0)
    result = _detect_anomalies(pts, threshold=1.5, window=30)
    assert isinstance(result, list)


def test_detect_anomalies_short_series_no_variance():
    pts = _ts_series([5.0, 5.0, 5.0])
    result = _detect_anomalies(pts)
    assert result == []


def test_detect_anomalies_datetime_format():
    vals = [10.0 + (i % 3) * 0.5 for i in range(50)]
    vals[35] = 9999.0
    pts = _ts_series(vals, start=1000000.0)
    result = _detect_anomalies(pts, threshold=2.0)
    if result:
        assert "UTC" in result[0]["datetime"]


# ── _detect_patterns ─────────────────────────────────────────────────────────


def test_detect_patterns_insufficient_data():
    pts = _ts_series([1.0] * 10)
    r = _detect_patterns(pts)
    assert r.get("insufficient_data") is True


def test_detect_patterns_returns_structure():
    # Need at least 24 points with real timestamps
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    pts = [(base + i * 3600, float(10 + (i % 5))) for i in range(48)]
    r = _detect_patterns(pts)
    assert "hourly_avg" in r
    assert "daily_avg" in r
    assert "peak_hour" in r
    assert "peak_day" in r


def test_detect_patterns_biz_vs_off():
    base = datetime(2026, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp()  # Monday
    pts = [(base + i * 3600, float(i % 24)) for i in range(72)]
    r = _detect_patterns(pts)
    assert "business_hours_mean" in r
    assert "off_hours_mean" in r


# ── _format_report ────────────────────────────────────────────────────────────

_EMPTY_PATTERNS = {"insufficient_data": True}


def test_format_report_basic():
    stats = {
        "n": 100, "mean": 50.0, "std": 5.0, "min": 30.0, "max": 70.0,
        "p05": 32.0, "p95": 68.0, "trend_pct": 2.5, "trend_dir": "stable", "cv": 10.0,
    }
    text = _format_report("cpu", "%", "24h", [], stats, [], _EMPTY_PATTERNS, "prometheus")
    assert "cpu" in text.lower()
    assert "24h" in text
    assert "50.0" in text
    assert "prometheus" in text.lower()


def test_format_report_with_anomalies():
    stats = {
        "n": 50, "mean": 10.0, "std": 2.0, "min": 5.0, "max": 20.0,
        "p05": 6.0, "p95": 19.0, "trend_pct": 10.0, "trend_dir": "rising", "cv": 20.0,
    }
    anomalies = [
        {"datetime": "2026-01-01 12:00 UTC", "value": 90.0, "z_score": 3.5, "direction": "high"},
        {"datetime": "2026-01-01 13:00 UTC", "value": 1.0, "z_score": -3.0, "direction": "low"},
    ]
    text = _format_report("memory", "MB", "6h", [], stats, anomalies, _EMPTY_PATTERNS, "postgres")
    assert "anomal" in text.lower()
    assert "spike" in text.lower()
    assert "drop" in text.lower()


def test_format_report_no_anomalies_message():
    stats = {
        "n": 10, "mean": 5.0, "std": 0.5, "min": 4.0, "max": 6.0,
        "p05": 4.1, "p95": 5.9, "trend_pct": 0.0, "trend_dir": "stable", "cv": 10.0,
    }
    text = _format_report("disk", "GB", "1h", [], stats, [], _EMPTY_PATTERNS, "local")
    assert "anomal" in text.lower()


def test_format_report_with_patterns():
    stats = {
        "n": 10, "mean": 5.0, "std": 1.0, "min": 3.0, "max": 8.0,
        "p05": 3.5, "p95": 7.5, "trend_pct": 1.0, "trend_dir": "stable", "cv": 20.0,
    }
    patterns = {
        "peak_hour": 14,
        "peak_hour_label": "14:00 UTC",
        "trough_hour": 4,
        "trough_hour_label": "04:00 UTC",
        "peak_day": "Monday",
        "trough_day": "Sunday",
        "business_hours_mean": 6.0,
        "off_hours_mean": 4.0,
        "biz_vs_off_ratio": 1.5,
        "hourly_avg": {14: 6.0, 4: 4.0},
        "daily_avg": {"Monday": 6.0, "Sunday": 4.0},
    }
    text = _format_report("traffic", "req/s", "7d", [], stats, [], patterns, "nginx")
    assert isinstance(text, str)
    assert len(text) > 100


def test_format_report_truncates_many_anomalies():
    """More than 10 anomalies should show a truncation note."""
    stats = {
        "n": 200, "mean": 5.0, "std": 1.0, "min": 1.0, "max": 10.0,
        "p05": 2.0, "p95": 9.0, "trend_pct": 0.0, "trend_dir": "stable", "cv": 20.0,
    }
    anomalies = [
        {"datetime": f"2026-01-01 {h:02d}:00 UTC", "value": 99.0, "z_score": 4.0, "direction": "high"}
        for h in range(15)
    ]
    text = _format_report("cpu", "%", "24h", [], stats, anomalies, _EMPTY_PATTERNS, "prometheus")
    assert "more" in text


# ── DataIntelligenceSkill metadata ────────────────────────────────────────────


def test_data_intelligence_skill_name():
    assert DataIntelligenceSkill.name == "data_intelligence"


def test_data_intelligence_skill_trigger_intents():
    assert "data_intelligence" in DataIntelligenceSkill.trigger_intents


def test_data_intelligence_skill_is_available():
    skill = DataIntelligenceSkill()
    # Should return bool without crashing (Prometheus may or may not be up)
    result = skill.is_available()
    assert isinstance(result, bool)


async def test_data_intelligence_execute_unknown_action():
    from unittest.mock import AsyncMock, patch
    skill = DataIntelligenceSkill()
    with patch(
        "app.integrations.prometheus_client.PrometheusClient.get_metric",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await skill.execute({"action": "unknown"}, "test")
    assert result is not None
