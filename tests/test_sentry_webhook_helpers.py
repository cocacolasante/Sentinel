"""
Extra tests for sentry_webhook pure helpers and task_board direct endpoint calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


# ── sentry_webhook._save_sentry_issue ─────────────────────────────────────────


def test_save_sentry_issue_success():
    from app.router.sentry_webhook import _save_sentry_issue
    with patch("app.db.postgres.execute") as mock_exec:
        _save_sentry_issue(
            issue_id="PROJ-123",
            title="TypeError: NoneType has no attribute x",
            level="error",
            status="unresolved",
            project="sentinel",
            permalink="https://sentry.io/issues/PROJ-123",
            count=5,
            platform="python",
            first_seen="2026-03-01T10:00:00Z",
            category="error",
        )
    mock_exec.assert_called_once()


def test_save_sentry_issue_db_exception():
    from app.router.sentry_webhook import _save_sentry_issue
    with patch("app.db.postgres.execute", side_effect=Exception("db down")):
        # Should not raise
        _save_sentry_issue("ID", "title", "error", "unresolved", "proj", "url", 1, "python", "2026", "error")


def test_create_pending_task_success():
    from app.router.sentry_webhook import _create_pending_task
    with patch("app.db.postgres.execute") as mock_exec:
        _create_pending_task(
            task_id="sentry-PROJ-123",
            title="Fix TypeError in auth module",
            params={"issue_id": "PROJ-123", "level": "error"},
            category="error",
        )
    mock_exec.assert_called_once()


def test_create_pending_task_db_exception():
    from app.router.sentry_webhook import _create_pending_task
    with patch("app.db.postgres.execute", side_effect=Exception("db down")):
        # Should not raise
        _create_pending_task("task-1", "title", {}, "error")


# ── sentry_webhook._maybe_slack_alert ────────────────────────────────────────


async def test_maybe_slack_alert_low_level_no_post():
    from app.router.sentry_webhook import _maybe_slack_alert
    with patch("app.integrations.slack_notifier.post_alert", new=AsyncMock()) as mock_alert:
        await _maybe_slack_alert(
            level="info",
            title="Some info message",
            project="sentinel",
            count=1,
            permalink="https://sentry.io/issues/1",
            issue_id="PROJ-1",
        )
    mock_alert.assert_not_called()


async def test_maybe_slack_alert_critical_posts():
    from app.router.sentry_webhook import _maybe_slack_alert
    with patch("app.integrations.slack_notifier.post_alert", new=AsyncMock()) as mock_alert:
        await _maybe_slack_alert(
            level="critical",
            title="Critical database failure",
            project="sentinel",
            count=50,
            permalink="https://sentry.io/issues/PROJ-123",
            issue_id="PROJ-123",
        )
    mock_alert.assert_called_once()


async def test_maybe_slack_alert_error_posts():
    from app.router.sentry_webhook import _maybe_slack_alert
    with patch("app.integrations.slack_notifier.post_alert", new=AsyncMock()) as mock_alert:
        await _maybe_slack_alert(
            level="error",
            title="Connection refused",
            project="sentinel",
            count=10,
            permalink="https://sentry.io/issues/123",
            issue_id="123",
        )
    mock_alert.assert_called_once()


# ── task_board router direct calls ───────────────────────────────────────────


async def test_task_board_list_tasks_empty():
    from app.router.task_board import list_tasks
    with patch("app.router.task_board.postgres") as mock_pg:
        mock_pg.execute.return_value = []
        result = await list_tasks(status=None, priority=None, limit=20)
    assert result["tasks"] == []
    assert result["count"] == 0


async def test_task_board_list_tasks_with_status():
    from app.router.task_board import list_tasks
    rows = [{"id": 1, "title": "Fix bug", "status": "pending", "priority_num": 3, "approval_level": 1}]
    with patch("app.router.task_board.postgres") as mock_pg:
        mock_pg.execute.return_value = rows
        result = await list_tasks(status="pending", priority=None, limit=20)
    assert result["count"] == 1


async def test_task_board_list_tasks_with_priority():
    from app.router.task_board import list_tasks
    with patch("app.router.task_board.postgres") as mock_pg:
        mock_pg.execute.return_value = []
        result = await list_tasks(status=None, priority=4, limit=10)
    assert result["tasks"] == []


async def test_task_board_get_task_found():
    from app.router.task_board import get_task
    row = {"id": 5, "title": "Deploy v2", "status": "in_progress", "priority_num": 4, "approval_level": 2}
    with patch("app.router.task_board.postgres") as mock_pg:
        mock_pg.execute_one.return_value = row
        result = await get_task(5)
    assert result["id"] == 5


async def test_task_board_get_task_not_found():
    from fastapi import HTTPException
    from app.router.task_board import get_task
    with patch("app.router.task_board.postgres") as mock_pg:
        mock_pg.execute_one.return_value = None
        try:
            await get_task(9999)
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 404


async def test_task_board_cancel_task():
    from app.router.task_board import cancel_task
    row = {"id": 5, "title": "Fix bug", "status": "cancelled"}
    with patch("app.router.task_board.postgres") as mock_pg, \
         patch("app.integrations.task_notifier.notify_status", new=AsyncMock()):
        mock_pg.execute_one.return_value = row
        result = await cancel_task(5)
    assert "cancelled" in result.get("message", "") or result.get("task", {}).get("id") == 5


async def test_task_board_cancel_task_not_found():
    from fastapi import HTTPException
    from app.router.task_board import cancel_task
    with patch("app.router.task_board.postgres") as mock_pg:
        mock_pg.execute_one.return_value = None
        try:
            await cancel_task(9999)
            assert False, "Should have raised"
        except HTTPException as e:
            assert e.status_code == 404


async def test_task_board_create_task():
    from app.router.task_board import create_task, TaskCreate
    row = {
        "id": 10, "title": "New task", "status": "pending",
        "priority_num": 3, "approval_level": 1,
        "description": "do it", "due_date": None, "source": "api",
        "tags": None, "assigned_to": None, "blocked_by": [],
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }
    body = TaskCreate(title="New task", description="do it")
    with patch("app.router.task_board.postgres") as mock_pg, \
         patch("app.integrations.task_notifier.post_task_created", new=AsyncMock()):
        mock_pg.execute_one.return_value = row
        result = await create_task(body)
    assert result["id"] == 10
