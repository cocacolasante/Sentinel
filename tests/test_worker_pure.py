"""
Tests for pure helper functions in app/worker/tasks.py and app/worker/rmm_tasks.py.

All DB and external service calls are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ── _touches_workspace ────────────────────────────────────────────────────────


def test_touches_workspace_true():
    from app.worker.tasks import _touches_workspace
    assert _touches_workspace(["cd /root/sentinel-workspace", "ls"]) is True


def test_touches_workspace_false():
    from app.worker.tasks import _touches_workspace
    assert _touches_workspace(["ls /tmp", "echo hello"]) is False


def test_touches_workspace_empty():
    from app.worker.tasks import _touches_workspace
    assert _touches_workspace([]) is False


def test_touches_workspace_none_in_list():
    from app.worker.tasks import _touches_workspace
    assert _touches_workspace([None, "echo hi"]) is False


def test_touches_workspace_mixed():
    from app.worker.tasks import _touches_workspace
    assert _touches_workspace(["echo foo", "/root/sentinel-workspace/start.sh"]) is True


# ── _mark_task ────────────────────────────────────────────────────────────────


def test_mark_task_in_progress():
    from app.worker.tasks import _mark_task
    with patch("app.db.postgres.execute") as mock_exec, \
         patch("app.integrations.task_notifier.notify_status_sync") as mock_notify, \
         patch("app.integrations.task_notifier._get_task_title", return_value="Test task"):
        _mark_task(1, "in_progress")
    mock_exec.assert_called_once()


def test_mark_task_done_no_dependents():
    from app.worker.tasks import _mark_task
    with patch("app.db.postgres.execute") as mock_exec:
        # First call: UPDATE, second call: SELECT dependents (returns empty)
        mock_exec.side_effect = [None, []]
        _mark_task(2, "done")
    assert mock_exec.call_count >= 1


def test_mark_task_failed_with_error():
    from app.worker.tasks import _mark_task
    with patch("app.db.postgres.execute") as mock_exec, \
         patch("app.integrations.task_notifier.notify_report_sync"), \
         patch("app.integrations.task_notifier._get_task_title", return_value="Failing task"):
        _mark_task(3, "failed", error="Connection refused")
    mock_exec.assert_called()


def test_mark_task_db_exception_doesnt_raise():
    from app.worker.tasks import _mark_task
    with patch("app.db.postgres.execute", side_effect=Exception("db down")):
        # Should not raise even if DB fails
        _mark_task(99, "in_progress")


# ── _unblock_dependents ───────────────────────────────────────────────────────


def test_unblock_dependents_no_dependents():
    from app.worker.tasks import _unblock_dependents
    with patch("app.db.postgres.execute", return_value=[]):
        _unblock_dependents(10)  # Should not raise


def test_unblock_dependents_still_blocked():
    from app.worker.tasks import _unblock_dependents
    dependents = [{
        "id": 20, "execution_queue": "tasks_general",
        "commands": '["echo hello"]', "approval_level": 1,
        "blocked_by": "[10, 15]",  # task 15 still pending
    }]
    with patch("app.db.postgres.execute", side_effect=[
        dependents,  # find dependents query
        [{"id": 10, "status": "done"}, {"id": 15, "status": "pending"}],  # blocker statuses
    ]):
        _unblock_dependents(10)  # Task 15 still pending, so 20 stays blocked


def test_unblock_dependents_db_exception():
    from app.worker.tasks import _unblock_dependents
    with patch("app.db.postgres.execute", side_effect=Exception("db error")):
        _unblock_dependents(5)  # Should not raise


# ── rmm_tasks helpers ─────────────────────────────────────────────────────────


def test_extract_ip_from_host_field():
    from app.worker.rmm_tasks import _extract_ip
    assert _extract_ip({"host": "192.168.1.100"}) == "192.168.1.100"


def test_extract_ip_from_netif():
    from app.worker.rmm_tasks import _extract_ip
    dev = {"netif": [{"addrs": ["10.0.0.1"]}]}
    assert _extract_ip(dev) == "10.0.0.1"


def test_extract_ip_empty_dict():
    from app.worker.rmm_tasks import _extract_ip
    assert _extract_ip({}) == ""


def test_extract_os_windows():
    from app.worker.rmm_tasks import _extract_os
    result = _extract_os({"ostype": 1})
    assert "Windows" in result


def test_extract_os_linux():
    from app.worker.rmm_tasks import _extract_os
    result = _extract_os({"ostype": 2})
    assert "Linux" in result


def test_extract_os_macos():
    from app.worker.rmm_tasks import _extract_os
    result = _extract_os({"ostype": 3})
    assert "macOS" in result


def test_extract_os_from_desc():
    from app.worker.rmm_tasks import _extract_os
    result = _extract_os({"osdesc": "Ubuntu 22.04"})
    assert "Ubuntu" in result


def test_extract_os_unknown():
    from app.worker.rmm_tasks import _extract_os
    assert _extract_os({}) == "unknown"


def test_infer_group_prod():
    from app.worker.rmm_tasks import _infer_group
    assert _infer_group({"name": "prod-web01"}) == "production"


def test_infer_group_staging():
    from app.worker.rmm_tasks import _infer_group
    assert _infer_group({"name": "staging-api"}) == "staging"


def test_infer_group_dev():
    from app.worker.rmm_tasks import _infer_group
    assert _infer_group({"name": "dev-worker"}) == "dev"


def test_infer_group_fallback():
    from app.worker.rmm_tasks import _infer_group
    assert _infer_group({}) == ""


def test_infer_project_sentinel():
    from app.worker.rmm_tasks import _infer_project
    assert _infer_project({"name": "sentinel-worker-01"}) == "sentinel"


def test_infer_project_empty():
    from app.worker.rmm_tasks import _infer_project
    assert _infer_project({}) == ""


def test_rmm_tasks_fmt_ts_none():
    from app.worker.rmm_tasks import _fmt_ts
    assert _fmt_ts(None) == "N/A"


def test_rmm_tasks_fmt_ts_string():
    from app.worker.rmm_tasks import _fmt_ts
    result = _fmt_ts("2026-01-01T10:00:00")
    assert "2026" in result


def test_rmm_tasks_fmt_ts_datetime():
    from app.worker.rmm_tasks import _fmt_ts
    from datetime import datetime, timezone
    dt = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    result = _fmt_ts(dt)
    assert "2026" in result


# ── post_alert_sync ───────────────────────────────────────────────────────────


def test_post_alert_sync_no_crash():
    from app.worker.tasks import post_alert_sync
    with patch("app.integrations.slack_notifier.post_alert_sync") as mock_alert:
        post_alert_sync("test alert")
    mock_alert.assert_called_once()
