"""
Tests for:
- app/learning/feedback_store.py (FeedbackStore)
- app/skills/project_skill.py (pure helpers and early-exit paths)
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, AsyncMock, patch


# ── learning/feedback_store.py ────────────────────────────────────────────────


def _make_mock_conn(fetchone_result=None, fetchall_result=None):
    """Create a full mock psycopg2 connection context manager."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=None)
    mock_cursor.fetchone.return_value = fetchone_result
    mock_cursor.fetchall.return_value = fetchall_result or []

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=None)
    mock_conn.cursor.return_value = mock_cursor

    return mock_conn


def test_feedback_store_store_rating_success():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    mock_conn = _make_mock_conn(fetchone_result=(42,))
    with patch.object(store, "_connect", return_value=mock_conn):
        result = store.store_rating("s1", 0, 6, "ok", "chat")
    assert result == 42


def test_feedback_store_store_rating_high_triggers_qdrant():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    mock_conn = _make_mock_conn(fetchone_result=(99,))
    with patch.object(store, "_connect", return_value=mock_conn), \
         patch.object(store, "_seed_qdrant") as mock_seed:
        result = store.store_rating("s1", 0, 9, "excellent", "code")
    mock_seed.assert_called_once_with(99, "s1", 0, 9, "code")


def test_feedback_store_store_rating_db_exception():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    with patch.object(store, "_connect", side_effect=Exception("conn failed")):
        result = store.store_rating("s1", 0, 5, None, "chat")
    assert result == -1


def test_feedback_store_get_avg_rating_overall():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    mock_conn = _make_mock_conn(fetchone_result=(7.5,))
    with patch.object(store, "_connect", return_value=mock_conn):
        result = store.get_avg_rating()
    assert result == 7.5


def test_feedback_store_get_avg_rating_by_intent():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    mock_conn = _make_mock_conn(fetchone_result=(8.0,))
    with patch.object(store, "_connect", return_value=mock_conn):
        result = store.get_avg_rating(intent="code")
    assert result == 8.0


def test_feedback_store_get_avg_rating_none():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    mock_conn = _make_mock_conn(fetchone_result=(None,))
    with patch.object(store, "_connect", return_value=mock_conn):
        result = store.get_avg_rating()
    assert result == 0.0


def test_feedback_store_get_avg_rating_exception():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    with patch.object(store, "_connect", side_effect=Exception("db down")):
        result = store.get_avg_rating()
    assert result == 0.0


def test_feedback_store_get_high_quality_interactions():
    from app.learning.feedback_store import FeedbackStore
    import psycopg2.extras
    store = FeedbackStore("postgresql://localhost/test")
    mock_rows = [{"session_id": "s1", "message_index": 0, "rating": 9, "comment": "great", "intent": "code", "created_at": "now"}]
    # Use RealDictRow-like objects
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=None)
    mock_cursor.fetchall.return_value = [mock_rows[0]]
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=None)
    mock_conn.cursor.return_value = mock_cursor
    with patch.object(store, "_connect", return_value=mock_conn):
        result = store.get_high_quality_interactions()
    assert len(result) == 1


def test_feedback_store_get_high_quality_exception():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    with patch.object(store, "_connect", side_effect=Exception("db down")):
        result = store.get_high_quality_interactions()
    assert result == []


def test_feedback_store_get_summary_success():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    mock_row = {"total_ratings": 50, "avg_rating": 7.2, "unique_sessions": 20, "top_intent": "code"}
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=None)
    mock_cursor.fetchone.return_value = mock_row
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=None)
    mock_conn.cursor.return_value = mock_cursor
    with patch.object(store, "_connect", return_value=mock_conn):
        result = store.get_summary()
    assert result["total_ratings"] == 50
    assert result["avg_rating"] == 7.2
    assert result["top_intent"] == "code"


def test_feedback_store_get_summary_exception():
    from app.learning.feedback_store import FeedbackStore
    store = FeedbackStore("postgresql://localhost/test")
    with patch.object(store, "_connect", side_effect=Exception("db down")):
        result = store.get_summary()
    assert result["total_ratings"] == 0
    assert result["avg_rating"] == 0.0


# ── skills/project_skill.py — metadata + early exits ─────────────────────────


def test_project_skill_metadata():
    from app.skills.project_skill import ProjectSkill
    s = ProjectSkill()
    assert s.name == "project"
    assert s.is_available() is True


async def test_project_skill_list_no_db():
    from app.skills.project_skill import ProjectSkill
    with patch("app.db.postgres.execute", return_value=[]):
        r = await ProjectSkill().execute({"action": "list"}, "list projects")
    assert isinstance(r.context_data, str)


async def test_project_skill_list_with_projects():
    from app.skills.project_skill import ProjectSkill
    rows = [{
        "id": 1, "name": "Sentinel", "slug": "sentinel", "status": "deployed",
        "tech_stack": "python", "server_ip": "1.2.3.4", "created_at": "2026-01-01",
    }]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await ProjectSkill().execute({"action": "list"}, "list projects")
    assert "Sentinel" in r.context_data or "sentinel" in r.context_data.lower()


async def test_project_skill_status_not_found():
    from app.skills.project_skill import ProjectSkill
    with patch("app.skills.project_skill._ensure_table"), \
         patch("app.db.postgres.execute_one", return_value=None):
        r = await ProjectSkill().execute({"action": "status", "name": "nonexistent"}, "project status")
    assert isinstance(r.context_data, str)


async def test_project_skill_status_found():
    from app.skills.project_skill import ProjectSkill
    row = {
        "id": 1, "name": "Sentinel", "slug": "sentinel", "status": "deployed",
        "tech_stack": "python", "server_ip": "10.0.0.1", "server_id": "srv123",
        "datacenter_id": "dc1", "created_at": "2026-01-01", "updated_at": "2026-01-02",
        "github_repo": "org/sentinel",
    }
    with patch("app.skills.project_skill._ensure_table"), \
         patch("app.db.postgres.execute_one", return_value=row):
        r = await ProjectSkill().execute({"action": "status", "name": "sentinel"}, "project status")
    assert "Sentinel" in r.context_data or "deployed" in r.context_data.lower()


async def test_project_skill_create_missing_name():
    from app.skills.project_skill import ProjectSkill
    with patch("app.skills.project_skill._ensure_table"):
        r = await ProjectSkill().execute({"action": "create"}, "create a project")
    assert isinstance(r.context_data, str)


async def test_project_skill_create_with_name():
    from app.skills.project_skill import ProjectSkill
    mock_task = MagicMock()
    mock_task.id = "fake-celery-id"
    with patch("app.skills.project_skill._ensure_table"), \
         patch("app.db.postgres.execute_one", return_value={"id": 1, "name": "MyApp", "slug": "myapp", "status": "queued", "tech_stack": "python", "path": "/tmp/myapp"}), \
         patch("app.worker.project_tasks.build_project") as mock_build:
        mock_build.apply_async.return_value = mock_task
        r = await ProjectSkill().execute(
            {"action": "create", "name": "MyApp", "tech_stack": "python"},
            "create myapp",
        )
    assert isinstance(r.context_data, str)


async def test_project_skill_deploy_missing_name():
    from app.skills.project_skill import ProjectSkill
    with patch("app.skills.project_skill._ensure_table"), \
         patch("app.db.postgres.execute_one", return_value=None):
        r = await ProjectSkill().execute({"action": "deploy"}, "deploy project")
    assert isinstance(r.context_data, str)


async def test_project_skill_unknown_action():
    from app.skills.project_skill import ProjectSkill
    with patch("app.skills.project_skill._ensure_table"), \
         patch("app.db.postgres.execute_one", return_value={"id": 1, "name": "MyApp", "slug": "myapp", "status": "queued"}):
        r = await ProjectSkill().execute({"action": "unknown_xyz"}, "do something")
    assert isinstance(r.context_data, str)
