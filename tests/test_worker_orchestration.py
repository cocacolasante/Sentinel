"""Tests for worker task orchestration — _mark_task, _unblock_dependents."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch


def _make_postgres_mock(dependents=None, statuses=None, output_rows=None, dep_row=None):
    """Build a postgres mock with configurable execute/execute_one behavior."""
    mock_pg = MagicMock()

    call_state = {"execute_count": 0}

    def execute_side_effect(sql, params=None):
        call_state["execute_count"] += 1
        sql_lower = sql.lower()
        if "blocked_by @>" in sql_lower:
            return dependents or []
        if "select id, status from tasks" in sql_lower:
            return statuses or []
        if "select title, output from tasks" in sql_lower:
            return output_rows or []
        return []

    def execute_one_side_effect(sql, params=None):
        if "select description" in sql.lower():
            return dep_row or {"description": "original description"}
        return None

    mock_pg.execute.side_effect = execute_side_effect
    mock_pg.execute_one.side_effect = execute_one_side_effect
    return mock_pg


def test_mark_task_stores_output():
    """_mark_task passes output to the SQL UPDATE when provided."""
    with patch("app.db.postgres.execute") as mock_execute, \
         patch("app.worker.tasks._unblock_dependents"):
        from app.worker.tasks import _mark_task

        _mark_task(99, "done", output="step output here")

    update_call = mock_execute.call_args
    assert update_call is not None
    sql, params = update_call[0]
    assert "output" in sql.lower()
    assert "step output here" in params


def test_mark_task_no_output_preserves_existing():
    """_mark_task with output=None uses COALESCE to preserve existing output."""
    with patch("app.db.postgres.execute") as mock_execute, \
         patch("app.worker.tasks._unblock_dependents"):
        from app.worker.tasks import _mark_task

        _mark_task(99, "done")

    update_call = mock_execute.call_args
    sql, params = update_call[0]
    assert "coalesce" in sql.lower()
    # None is passed so COALESCE keeps existing value
    assert params[1] is None


def test_unblock_dependents_dispatches_commands_task():
    """_unblock_dependents enqueues execute_board_task for tasks WITH commands."""
    dep_id = 20
    dep = {
        "id": dep_id,
        "blocked_by": json.dumps([10]),
        "commands": json.dumps(["echo hello"]),
        "approval_level": 1,
        "execution_queue": "tasks_general",
    }

    mock_pg = MagicMock()
    mock_pg.execute.side_effect = [
        [dep],                          # SELECT dependents
        [{"id": 10, "status": "done"}], # SELECT blocker statuses
        [],                             # SELECT title,output (blocker context)
        None,                           # UPDATE celery_task_id
    ]
    mock_pg.execute_one.return_value = {"description": ""}

    mock_result = MagicMock()
    mock_result.id = "celery-abc"

    with patch("app.db.postgres.execute", mock_pg.execute), \
         patch("app.db.postgres.execute_one", mock_pg.execute_one), \
         patch("app.worker.tasks.execute_board_task") as mock_exec, \
         patch("app.worker.tasks.plan_and_execute_board_task") as mock_plan:

        mock_exec.apply_async.return_value = mock_result

        from app.worker.tasks import _unblock_dependents
        _unblock_dependents(10)

    mock_exec.apply_async.assert_called_once_with(args=[dep_id], queue="tasks_general")
    mock_plan.apply_async.assert_not_called()


def test_unblock_dependents_dispatches_no_commands_task():
    """_unblock_dependents enqueues plan_and_execute_board_task for tasks WITHOUT commands (bug fix)."""
    dep_id = 21
    dep = {
        "id": dep_id,
        "blocked_by": json.dumps([10]),
        "commands": json.dumps([]),
        "approval_level": 1,
        "execution_queue": "tasks_general",
    }

    mock_pg = MagicMock()
    mock_pg.execute.side_effect = [
        [dep],                          # SELECT dependents
        [{"id": 10, "status": "done"}], # SELECT blocker statuses
        [],                             # SELECT title,output (blocker context)
        None,                           # UPDATE celery_task_id
    ]
    mock_pg.execute_one.return_value = {"description": ""}

    mock_result = MagicMock()
    mock_result.id = "celery-xyz"

    with patch("app.db.postgres.execute", mock_pg.execute), \
         patch("app.db.postgres.execute_one", mock_pg.execute_one), \
         patch("app.worker.tasks.execute_board_task") as mock_exec, \
         patch("app.worker.tasks.plan_and_execute_board_task") as mock_plan:

        mock_plan.apply_async.return_value = mock_result

        from app.worker.tasks import _unblock_dependents
        _unblock_dependents(10)

    mock_plan.apply_async.assert_called_once_with(args=[dep_id], queue="tasks_general")
    mock_exec.apply_async.assert_not_called()


def test_unblock_dependents_injects_blocker_context():
    """_unblock_dependents injects blocker output into dependent task description."""
    dep_id = 22
    dep = {
        "id": dep_id,
        "blocked_by": json.dumps([10]),
        "commands": json.dumps([]),
        "approval_level": 1,
        "execution_queue": "tasks_general",
    }

    mock_pg = MagicMock()
    description_updates = []

    def execute_side_effect(sql, params=None):
        sql_lower = sql.lower()
        if "blocked_by @>" in sql_lower:
            return [dep]
        if "select id, status" in sql_lower:
            return [{"id": 10, "status": "done"}]
        if "select title, output" in sql_lower:
            return [{"title": "Audit task", "output": "found 3 issues"}]
        if "update tasks set description" in sql_lower:
            description_updates.append(params)
        return []

    mock_pg.execute.side_effect = execute_side_effect
    mock_pg.execute_one.return_value = {"description": "original"}

    mock_result = MagicMock()
    mock_result.id = "celery-ctx"

    with patch("app.db.postgres.execute", mock_pg.execute), \
         patch("app.db.postgres.execute_one", mock_pg.execute_one), \
         patch("app.worker.tasks.execute_board_task") as mock_exec, \
         patch("app.worker.tasks.plan_and_execute_board_task") as mock_plan:

        mock_plan.apply_async.return_value = mock_result

        from app.worker.tasks import _unblock_dependents
        _unblock_dependents(10)

    assert len(description_updates) >= 1
    updated_desc = description_updates[0][0]
    assert "found 3 issues" in updated_desc
    assert "Context from completed prerequisites" in updated_desc


def test_unblock_dependents_skips_still_blocked_tasks():
    """_unblock_dependents skips tasks that are still blocked by other tasks."""
    dep_id = 23
    dep = {
        "id": dep_id,
        "blocked_by": json.dumps([10, 11]),
        "commands": json.dumps([]),
        "approval_level": 1,
    }

    mock_pg = MagicMock()
    mock_pg.execute.side_effect = [
        [dep],                                                      # SELECT dependents
        [{"id": 10, "status": "done"}, {"id": 11, "status": "pending"}],  # blocker statuses
    ]

    with patch("app.db.postgres.execute", mock_pg.execute), \
         patch("app.db.postgres.execute_one", mock_pg.execute_one), \
         patch("app.worker.tasks.execute_board_task") as mock_exec, \
         patch("app.worker.tasks.plan_and_execute_board_task") as mock_plan:

        from app.worker.tasks import _unblock_dependents
        _unblock_dependents(10)

    mock_exec.apply_async.assert_not_called()
    mock_plan.apply_async.assert_not_called()


def test_scan_pending_tasks_routes_commands_vs_nocommands():
    """_scan_pending_tasks routes cmd tasks to execute_board_task and LLM tasks to plan_and_execute."""
    import asyncio

    cmd_task = {"id": 30, "title": "cmd task", "commands": json.dumps(["echo hi"]), "execution_queue": "tasks_general", "blocked_by": json.dumps([])}
    llm_task = {"id": 31, "title": "llm task", "commands": json.dumps([]), "execution_queue": "tasks_general", "blocked_by": json.dumps([])}

    mock_pg = MagicMock()
    mock_pg.execute.side_effect = [
        [cmd_task, llm_task],  # SELECT pending tasks
        None,                  # UPDATE celery_task_id for cmd_task
        None,                  # UPDATE celery_task_id for llm_task
    ]

    mock_result = MagicMock()
    mock_result.id = "cid"

    with patch("app.db.postgres.execute", mock_pg.execute), \
         patch("app.db.postgres.execute_one", mock_pg.execute_one), \
         patch("app.worker.tasks.execute_board_task") as mock_exec, \
         patch("app.worker.tasks.plan_and_execute_board_task") as mock_plan:

        mock_exec.apply_async.return_value = mock_result
        mock_plan.apply_async.return_value = mock_result

        from app.worker.tasks import _scan_pending_tasks
        result = asyncio.run(_scan_pending_tasks())

    assert result["dispatched"] == 2
    mock_exec.apply_async.assert_called_once_with(args=[30], queue="tasks_general")
    mock_plan.apply_async.assert_called_once_with(args=[31], queue="tasks_general")
