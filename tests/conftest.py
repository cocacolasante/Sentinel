"""
Shared pytest fixtures.

All external service connections (PostgreSQL, Redis, Qdrant, Slack) are mocked
so the test suite runs without any live infrastructure.
"""
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Set test env vars before any app module is imported.
# These must be set at module level so they take effect when app.config is
# first read (lru_cache means get_settings() runs exactly once per process).
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_DIR", "/tmp/aibrain-test-logs")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PASSWORD", "")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_USER", "brain")
os.environ.setdefault("POSTGRES_PASSWORD", "changeme")
os.environ.setdefault("POSTGRES_DB", "aibrain")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("SENTRY_DSN", "")
# Slack — dummy values so AsyncApp initialises without raising BoltError.
# start_socket_mode is patched in the client fixture so no real connection is made.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")


@pytest.fixture(scope="session")
def client():
    """
    FastAPI TestClient with all external services mocked.

    The three lifespan calls that would fail without live services are patched:
      - postgres.init_schema    (PostgreSQL schema setup)
      - QdrantMemory.init_collection  (Qdrant vector store setup)
      - start_socket_mode       (Slack Socket Mode)

    Individual test functions should patch endpoint-level dependencies
    (e.g. postgres.ping, RedisMemory.ping, Dispatcher.process) as needed.
    """
    with (
        patch("app.db.postgres.init_schema"),
        patch(
            "app.memory.qdrant_client.QdrantMemory.init_collection",
            new_callable=AsyncMock,
        ),
        patch(
            "app.router.slack.start_socket_mode",
            new_callable=AsyncMock,
        ),
    ):
        # Clear the lru_cache so the test env vars above take effect
        from app.config import get_settings
        get_settings.cache_clear()

        from app.main import app  # import after env vars + patches are set

        with TestClient(app, raise_server_exceptions=True) as c:
            yield c
