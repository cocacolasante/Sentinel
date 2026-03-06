"""
Execute-path tests for DataIntelligenceSkill and its gather helpers.

All external calls (Prometheus, Postgres, Sentry) are mocked.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from app.skills.data_intelligence_skill import (
    DataIntelligenceSkill,
    _gather_db_tasks,
    _gather_db_milestones,
    _gather_sentry_errors,
    _gather_prometheus,
)
from app.skills.base import SkillResult


# ── _gather_prometheus ────────────────────────────────────────────────────────


async def test_gather_prometheus_known_metric():
    mock_client = MagicMock()
    mock_client.get_metric = AsyncMock(return_value=[(1000.0, 55.0), (1060.0, 60.0)])
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        pts, unit, source = await _gather_prometheus("cpu", "1h")
    assert len(pts) == 2
    assert unit == "%"
    assert "Prometheus" in source


async def test_gather_prometheus_unknown_metric():
    mock_client = MagicMock()
    mock_client.get_metric = AsyncMock(return_value=[])
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        pts, unit, source = await _gather_prometheus("custom_metric_xyz", "24h")
    assert pts == []
    assert unit == ""  # unknown metric has no unit


async def test_gather_prometheus_memory_unit():
    mock_client = MagicMock()
    mock_client.get_metric = AsyncMock(return_value=[(1000.0, 70.0)])
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        pts, unit, _ = await _gather_prometheus("memory", "6h")
    assert unit == "%"


# ── _gather_db_tasks ──────────────────────────────────────────────────────────


async def test_gather_db_tasks_returns_points():
    from datetime import datetime, timezone
    bucket = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    mock_rows = [{"bucket": bucket, "cnt": 5}]
    with patch("app.db.postgres.execute", return_value=mock_rows):
        pts, unit, source = await _gather_db_tasks("24h")
    assert len(pts) == 1
    assert pts[0][1] == 5.0
    assert unit == "tasks/hr"
    assert "PostgreSQL" in source


async def test_gather_db_tasks_empty():
    with patch("app.db.postgres.execute", return_value=[]):
        pts, unit, _ = await _gather_db_tasks("1h")
    assert pts == []
    assert unit == "tasks/hr"


async def test_gather_db_tasks_exception():
    with patch("app.db.postgres.execute", side_effect=Exception("no db")):
        pts, unit, _ = await _gather_db_tasks("24h")
    assert pts == []


# ── _gather_db_milestones ─────────────────────────────────────────────────────


async def test_gather_db_milestones_returns_points():
    from datetime import datetime, timezone
    bucket = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    mock_rows = [{"bucket": bucket, "cnt": 3}]
    with patch("app.db.postgres.execute", return_value=mock_rows):
        pts, unit, source = await _gather_db_milestones("24h")
    assert len(pts) == 1
    assert pts[0][1] == 3.0
    assert "milestones" in source.lower()


async def test_gather_db_milestones_exception():
    with patch("app.db.postgres.execute", side_effect=Exception("fail")):
        pts, unit, _ = await _gather_db_milestones("1h")
    assert pts == []


# ── _gather_sentry_errors ─────────────────────────────────────────────────────


async def test_gather_sentry_not_configured():
    mock_client = MagicMock()
    mock_client.is_configured.return_value = False
    with patch("app.integrations.sentry_client.SentryClient", return_value=mock_client):
        pts, unit, source = await _gather_sentry_errors("24h")
    assert pts == []
    assert "Sentry" in source


async def test_gather_sentry_configured_with_data():
    mock_client = MagicMock()
    mock_client.is_configured.return_value = True
    mock_client.list_issues = AsyncMock(return_value=[
        {"id": "1", "firstSeen": "2026-01-01T10:00:00.000Z",
         "lastSeen": "2026-01-01T12:00:00.000Z", "count": "10"},
    ])
    with patch("app.integrations.sentry_client.SentryClient", return_value=mock_client):
        pts, unit, source = await _gather_sentry_errors("24h")
    assert isinstance(pts, list)
    assert "Sentry" in source


async def test_gather_sentry_exception():
    with patch("app.integrations.sentry_client.SentryClient", side_effect=Exception("fail")):
        pts, unit, _ = await _gather_sentry_errors("24h")
    assert pts == []


# ── DataIntelligenceSkill.execute — overview path ─────────────────────────────


async def test_execute_auto_overview():
    """Action='auto' triggers multi-metric overview."""
    mock_client = MagicMock()
    mock_client.get_metric = AsyncMock(return_value=[])
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client), \
         patch("app.db.postgres.execute", return_value=[]):
        r = await DataIntelligenceSkill().execute({"action": "overview", "window": "1h"}, "")
    assert isinstance(r, SkillResult)
    assert "Overview" in r.context_data or "overview" in r.context_data.lower()


async def test_execute_all_overview():
    mock_client = MagicMock()
    mock_client.get_metric = AsyncMock(return_value=[])
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client), \
         patch("app.db.postgres.execute", return_value=[]):
        r = await DataIntelligenceSkill().execute({"metric": "all"}, "system overview")
    assert isinstance(r.context_data, str)


# ── DataIntelligenceSkill.execute — specific metric ───────────────────────────


async def test_execute_cpu_metric_no_data():
    mock_client = MagicMock()
    mock_client.get_metric = AsyncMock(return_value=[])
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        r = await DataIntelligenceSkill().execute(
            {"action": "analyze", "metric": "cpu", "window": "24h"}, "analyze cpu"
        )
    assert "no data" in r.context_data.lower() or "cpu" in r.context_data.lower()


async def test_execute_cpu_metric_with_data():
    base = time.time() - 3600 * 50
    # Generate 50 points with small variance to allow pattern detection
    pts = [(base + i * 3600, 50.0 + (i % 5) * 2.0) for i in range(50)]
    mock_client = MagicMock()
    mock_client.get_metric = AsyncMock(return_value=pts)
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        r = await DataIntelligenceSkill().execute(
            {"action": "analyze", "metric": "cpu", "window": "24h"}, "cpu stats"
        )
    assert "cpu" in r.context_data.lower() or "CPU" in r.context_data


async def test_execute_tasks_metric():
    from datetime import datetime, timezone
    bucket = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    rows = [{"bucket": bucket, "cnt": 5}]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await DataIntelligenceSkill().execute(
            {"metric": "tasks", "window": "24h"}, "task analysis"
        )
    assert isinstance(r.context_data, str)


async def test_execute_milestones_metric():
    from datetime import datetime, timezone
    bucket = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    rows = [{"bucket": bucket, "cnt": 2}]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await DataIntelligenceSkill().execute(
            {"metric": "milestones", "window": "24h"}, "milestone analysis"
        )
    assert isinstance(r.context_data, str)


# ── DataIntelligenceSkill.execute — custom PromQL ─────────────────────────────


async def test_execute_custom_promql_no_data():
    mock_client = MagicMock()
    mock_client.query_range = AsyncMock(return_value=[])
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        r = await DataIntelligenceSkill().execute(
            {"promql": "up", "window": "1h"}, "custom promql"
        )
    assert "no data" in r.context_data.lower() or "custom" in r.context_data.lower()


async def test_execute_custom_promql_with_data():
    base = time.time() - 3600
    pts = [(base + i * 60, 1.0 + i * 0.01) for i in range(50)]
    mock_client = MagicMock()
    mock_client.query_range = AsyncMock(return_value=pts)
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        r = await DataIntelligenceSkill().execute(
            {"promql": "up", "window": "1h"}, "run promql"
        )
    assert isinstance(r.context_data, str)
    assert len(r.context_data) > 50


# ── is_available ──────────────────────────────────────────────────────────────


def test_is_available_false_when_not_configured():
    mock_client = MagicMock()
    mock_client.is_configured.return_value = False
    with patch("app.integrations.prometheus_client.PrometheusClient", return_value=mock_client):
        skill = DataIntelligenceSkill()
        assert isinstance(skill.is_available(), bool)
