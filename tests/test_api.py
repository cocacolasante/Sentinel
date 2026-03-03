"""
API integration tests.

Uses the `client` fixture from conftest.py which provides a TestClient
with all external services (PostgreSQL, Redis, Qdrant, Slack) mocked.
Endpoint-level dependencies are patched inside individual test functions.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Root endpoint ──────────────────────────────────────────────────────────────

def test_root_returns_alive(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "Brain is alive"
    assert data["version"] == "2.0.0"


# ── Health endpoint ────────────────────────────────────────────────────────────

def test_health_ok(client):
    with (
        patch("app.db.postgres.ping", return_value=True),
        patch("app.memory.redis_client.RedisMemory.ping", return_value=True),
    ):
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["redis"] is True
    assert data["postgres"] is True


def test_health_degraded_redis_down(client):
    with (
        patch("app.db.postgres.ping", return_value=True),
        patch("app.memory.redis_client.RedisMemory.ping", return_value=False),
    ):
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["redis"] is False
    assert data["postgres"] is True


def test_health_degraded_postgres_down(client):
    with (
        patch("app.db.postgres.ping", return_value=False),
        patch("app.memory.redis_client.RedisMemory.ping", return_value=True),
    ):
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["postgres"] is False


# ── Chat endpoint ──────────────────────────────────────────────────────────────

def test_chat_returns_reply(client):
    from app.brain.dispatcher import DispatchResult
    import app.router.chat as chat_module

    mock_result = DispatchResult(
        reply="Hello! How can I help?",
        intent="chat",
        session_id="test-session",
        agent="chat",
    )
    with patch.object(chat_module.dispatch, "process", new_callable=AsyncMock) as mock_proc:
        mock_proc.return_value = mock_result
        resp = client.post(
            "/api/v1/chat",
            json={"message": "Hello", "session_id": "test-session"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Hello! How can I help?"
    assert data["intent"] == "chat"
    assert data["session_id"] == "test-session"
    assert data["agent"] == "chat"


def test_chat_empty_session_uses_primary(client):
    """An empty session_id should be mapped to the primary shared session."""
    from app.brain.dispatcher import DispatchResult
    from app.config import Settings
    import app.router.chat as chat_module

    primary = Settings().brain_primary_session
    mock_result = DispatchResult(
        reply="Got it",
        intent="chat",
        session_id=primary,
        agent="chat",
    )
    with patch.object(chat_module.dispatch, "process", new_callable=AsyncMock) as mock_proc:
        mock_proc.return_value = mock_result
        resp = client.post("/api/v1/chat", json={"message": "Hi", "session_id": ""})

    assert resp.status_code == 200
    # Verify the dispatcher was called with the primary session, not ""
    called_session = mock_proc.call_args[0][1]
    assert called_session == primary


def test_chat_rejects_empty_message(client):
    resp = client.post("/api/v1/chat", json={"message": "", "session_id": "s1"})
    assert resp.status_code == 422  # Pydantic min_length=1 validation error


def test_chat_propagates_dispatcher_error(client):
    import app.router.chat as chat_module

    with patch.object(chat_module.dispatch, "process", new_callable=AsyncMock) as mock_proc:
        mock_proc.side_effect = RuntimeError("LLM unreachable")
        resp = client.post(
            "/api/v1/chat",
            json={"message": "Hello", "session_id": "s1"},
            # Don't raise — we want to check the 502 response
        )
    assert resp.status_code == 502


# ── Clear session endpoint ─────────────────────────────────────────────────────

def test_clear_session(client):
    import app.router.chat as chat_module

    with (
        patch.object(chat_module.memory, "clear_session"),
        patch.object(chat_module.memory, "clear_pending_action"),
    ):
        resp = client.delete("/api/v1/chat/my-session-id")

    assert resp.status_code == 200
    assert resp.json() == {"cleared": "my-session-id"}


# ── Agents list endpoint ───────────────────────────────────────────────────────

def test_list_agents(client):
    mock_registry = MagicMock()
    mock_registry.list_agents.return_value = [
        {"name": "chat", "description": "General conversation"},
    ]
    with patch("app.agents.registry.AgentRegistry", return_value=mock_registry):
        resp = client.get("/api/v1/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert "agents" in data
