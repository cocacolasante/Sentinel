"""
Tests for task_board router (_enrich helper) and deeper task_skill paths.

Router endpoint tests use mocked DB; FastAPI is not started.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── task_board._enrich ────────────────────────────────────────────────────────


def test_enrich_adds_labels():
    from app.router.task_board import _enrich
    row = {"id": 1, "title": "Fix bug", "priority_num": 5, "approval_level": 3}
    result = _enrich(row)
    assert result["priority_label"] == "Critical"
    assert result["approval_label"] == "requires sign-off"


def test_enrich_defaults_for_missing_priority():
    from app.router.task_board import _enrich
    row = {"id": 2, "title": "Task", "priority_num": None, "approval_level": None}
    result = _enrich(row)
    assert result["priority_label"] == "Normal"
    assert result["approval_label"] == "needs review"


def test_enrich_priority_1():
    from app.router.task_board import _enrich
    row = {"priority_num": 1, "approval_level": 1}
    result = _enrich(row)
    assert result["priority_label"] == "Low"
    assert result["approval_label"] == "auto-approve"


def test_enrich_priority_2():
    from app.router.task_board import _enrich
    result = _enrich({"priority_num": 2, "approval_level": 2})
    assert result["priority_label"] == "Minor"


def test_enrich_priority_4():
    from app.router.task_board import _enrich
    result = _enrich({"priority_num": 4, "approval_level": 1})
    assert result["priority_label"] == "High"


def test_enrich_does_not_mutate_original():
    from app.router.task_board import _enrich
    row = {"priority_num": 3, "approval_level": 2}
    original_keys = set(row.keys())
    _enrich(row)
    assert set(row.keys()) == original_keys  # original unchanged


# ── task_skill._fmt_task ───────────────────────────────────────────────────────


def test_fmt_task_basic():
    from app.skills.task_skill import _fmt_task
    row = {
        "id": 42, "title": "Deploy sentinel", "status": "pending",
        "priority": "high", "priority_num": 4, "approval_level": 2,
        "tags": None, "due_date": None, "assigned_to": None,
    }
    result = _fmt_task(row)
    assert "42" in result
    assert "Deploy sentinel" in result
    assert "pending" in result.lower()


def test_fmt_task_with_tags():
    from app.skills.task_skill import _fmt_task
    row = {
        "id": 10, "title": "Fix login", "status": "in_progress",
        "priority": "normal", "priority_num": 3, "approval_level": 1,
        "tags": '["auth", "bug"]', "due_date": "2026-03-15", "assigned_to": "alice",
    }
    result = _fmt_task(row)
    assert "10" in result
    assert "Fix login" in result


def test_fmt_task_with_list_tags():
    from app.skills.task_skill import _fmt_task
    row = {
        "id": 20, "title": "Migrate DB", "status": "done",
        "priority": "low", "priority_num": 2, "approval_level": 1,
        "tags": ["infra", "db"], "due_date": None, "assigned_to": None,
    }
    result = _fmt_task(row)
    assert "20" in result


def test_fmt_task_all_none_fields():
    from app.skills.task_skill import _fmt_task
    row = {
        "id": 99, "title": "Test task", "status": "cancelled",
        "priority": None, "priority_num": None, "approval_level": None,
        "tags": None, "due_date": None, "assigned_to": None,
    }
    result = _fmt_task(row)
    assert "99" in result


# ── TaskCreateSkill deeper paths ──────────────────────────────────────────────


async def test_task_create_with_title():
    from app.skills.task_skill import TaskCreateSkill
    with patch("app.db.postgres.execute_one") as mock_one:
        mock_one.return_value = {
            "id": 101, "title": "New task", "status": "pending",
            "priority_num": 3, "approval_level": 1,
            "created_at": "2026-01-01T00:00:00",
        }
        with patch("app.integrations.task_notifier.post_task_created") as mock_notify:
            mock_notify.return_value = None
            r = await TaskCreateSkill().execute(
                {"title": "New task", "description": "do something"},
                "create a task",
            )
    assert isinstance(r.context_data, str)


async def test_task_create_db_error():
    from app.skills.task_skill import TaskCreateSkill
    with patch("app.db.postgres.execute_one", side_effect=Exception("db down")):
        r = await TaskCreateSkill().execute(
            {"title": "Failing task"}, "create task"
        )
    assert isinstance(r.context_data, str)


async def test_task_create_missing_title():
    from app.skills.task_skill import TaskCreateSkill
    r = await TaskCreateSkill().execute({}, "create a task")
    assert isinstance(r.context_data, str)


# ── TaskReadSkill deeper paths ────────────────────────────────────────────────


async def test_task_read_get_specific():
    from app.skills.task_skill import TaskReadSkill
    row = {
        "id": 5, "title": "Fix auth", "description": "Auth is broken", "status": "in_progress",
        "priority": "high", "priority_num": 4, "approval_level": 2,
        "tags": None, "due_date": None, "assigned_to": None,
        "blocked_by": [], "created_at": "2026-01-01", "updated_at": "2026-01-02",
        "celery_task_id": None,
    }
    with patch("app.db.postgres.execute_one", return_value=row):
        r = await TaskReadSkill().execute({"action": "get", "id": "5"}, "show task 5")
    assert "Fix auth" in r.context_data


async def test_task_read_get_not_found():
    from app.skills.task_skill import TaskReadSkill
    with patch("app.db.postgres.execute_one", return_value=None):
        r = await TaskReadSkill().execute({"action": "get", "id": "999"}, "show task 999")
    assert "no task" in r.context_data.lower() or "999" in r.context_data


async def test_task_read_list_with_priority_filter():
    from app.skills.task_skill import TaskReadSkill
    with patch("app.db.postgres.execute", return_value=[]):
        r = await TaskReadSkill().execute({"action": "list", "priority": "4"}, "high priority tasks")
    assert isinstance(r.context_data, str)


# ── TaskUpdateSkill further paths ──────────────────────────────────────────────


async def test_task_update_task_not_found():
    from app.skills.task_skill import TaskUpdateSkill
    with patch("app.db.postgres.execute_one", return_value=None):
        r = await TaskUpdateSkill().execute({"id": 9999, "status": "done"}, "finish task")
    assert "no task" in r.context_data.lower() or "9999" in r.context_data


async def test_task_update_no_changes():
    from app.skills.task_skill import TaskUpdateSkill
    with patch("app.db.postgres.execute_one", return_value={
        "id": 5, "title": "Deploy v2", "status": "pending",
        "priority_num": 3, "approval_level": 1,
    }):
        r = await TaskUpdateSkill().execute({"id": 5}, "update task 5")
    assert "no changes" in r.context_data.lower()


async def test_task_update_title_change():
    from app.skills.task_skill import TaskUpdateSkill
    with patch("app.db.postgres.execute_one", return_value={
        "id": 5, "title": "Old title", "status": "pending",
        "priority_num": 3, "approval_level": 1,
    }):
        r = await TaskUpdateSkill().execute(
            {"id": 5, "title": "New title"}, "rename task"
        )
    assert r.pending_action is not None


async def test_task_update_priority_change():
    from app.skills.task_skill import TaskUpdateSkill
    with patch("app.db.postgres.execute_one", return_value={
        "id": 5, "title": "Fix things", "status": "pending",
        "priority_num": 3, "approval_level": 1,
    }):
        r = await TaskUpdateSkill().execute(
            {"id": 5, "priority": 5}, "make urgent"
        )
    assert r.pending_action is not None
