"""Unit tests for RateLimiter — mocked Redis, no live services needed."""

import pytest
from unittest.mock import MagicMock, patch

from app.brain.rate_limiter import RateLimiter, RateLimitExceeded


def _make_limiter(count_min: int, count_hour: int) -> RateLimiter:
    """Return a RateLimiter pre-wired with a mock Redis pipeline."""
    limiter = RateLimiter()
    mock_redis = MagicMock()
    pipe_mock = MagicMock()
    pipe_mock.execute.return_value = [count_min, count_hour]
    mock_redis.pipeline.return_value = pipe_mock
    limiter._redis = mock_redis
    return limiter


def _default_settings(per_minute: int = 20, per_hour: int = 200):
    s = MagicMock()
    s.rate_limit_per_minute = per_minute
    s.rate_limit_per_hour = per_hour
    return s


# ── Exception class ───────────────────────────────────────────────────────────


def test_rate_limit_exceeded_is_exception():
    with pytest.raises(RateLimitExceeded):
        raise RateLimitExceeded("over limit")


# ── check() happy path ────────────────────────────────────────────────────────


def test_check_passes_on_first_request():
    limiter = _make_limiter(count_min=1, count_hour=1)
    with patch("app.brain.rate_limiter.get_settings", return_value=_default_settings()):
        limiter.check("session-123")  # must not raise


def test_check_passes_at_limit_boundary():
    # Exactly at the limit should NOT raise (> limit raises, not >=)
    limiter = _make_limiter(count_min=20, count_hour=200)
    with patch("app.brain.rate_limiter.get_settings", return_value=_default_settings()):
        limiter.check("session-abc")  # must not raise


# ── check() failure paths ─────────────────────────────────────────────────────


def test_check_raises_when_minute_limit_exceeded():
    limiter = _make_limiter(count_min=21, count_hour=5)
    with patch("app.brain.rate_limiter.get_settings", return_value=_default_settings()):
        with pytest.raises(RateLimitExceeded, match="minute"):
            limiter.check("session-abc")


def test_check_raises_when_hour_limit_exceeded():
    limiter = _make_limiter(count_min=5, count_hour=201)
    with patch("app.brain.rate_limiter.get_settings", return_value=_default_settings()):
        with pytest.raises(RateLimitExceeded, match="hour"):
            limiter.check("session-abc")


# ── TTL behaviour ─────────────────────────────────────────────────────────────


def test_check_sets_ttl_on_first_increment():
    """expire() must be called for both keys when counts are 1 (first request)."""
    limiter = _make_limiter(count_min=1, count_hour=1)
    with patch("app.brain.rate_limiter.get_settings", return_value=_default_settings()):
        limiter.check("session-new")

    assert limiter._redis.expire.call_count == 2


def test_check_does_not_reset_ttl_on_subsequent_requests():
    """expire() must NOT be called when counts > 1 (TTL already set)."""
    limiter = _make_limiter(count_min=5, count_hour=50)
    with patch("app.brain.rate_limiter.get_settings", return_value=_default_settings()):
        limiter.check("session-existing")

    limiter._redis.expire.assert_not_called()
