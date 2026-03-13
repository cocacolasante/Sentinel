"""Unit tests for CostTracker — pure logic and mocked-Redis paths."""

import pytest
from unittest.mock import MagicMock, patch

from app.brain.cost_tracker import (
    BudgetExceeded,
    CostTracker,
    _parse_thresholds,
)


# ── _calc_usd (pure math, no external deps) ───────────────────────────────────


def test_calc_usd_sonnet_per_million():
    # $3.00 input + $15.00 output per 1M tokens
    cost = CostTracker._calc_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == pytest.approx(18.00)


def test_calc_usd_haiku_per_million():
    # $1.00 input + $5.00 output per 1M tokens
    cost = CostTracker._calc_usd("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert cost == pytest.approx(6.00)


def test_calc_usd_unknown_model_uses_fallback():
    # Fallback = Sonnet input rate ($3.00 / 1M)
    cost = CostTracker._calc_usd("gpt-unknown", 1_000_000, 0)
    assert cost == pytest.approx(3.00)


def test_calc_usd_zero_tokens():
    cost = CostTracker._calc_usd("claude-sonnet-4-6", 0, 0)
    assert cost == 0.0


def test_calc_usd_small_call():
    # 1 000 input + 500 output tokens at Sonnet rates
    expected = (1_000 * 3.00 + 500 * 15.00) / 1_000_000
    assert CostTracker._calc_usd("claude-sonnet-4-6", 1_000, 500) == pytest.approx(expected)


def test_calc_usd_output_only():
    cost = CostTracker._calc_usd("claude-sonnet-4-6", 0, 1_000_000)
    assert cost == pytest.approx(15.00)


# ── _parse_thresholds (pure parsing) ─────────────────────────────────────────


def test_parse_thresholds_normal():
    assert _parse_thresholds("0.5,0.8,1.0") == pytest.approx([0.5, 0.8, 1.0])


def test_parse_thresholds_empty_string():
    assert _parse_thresholds("") == []


def test_parse_thresholds_strips_whitespace():
    result = _parse_thresholds(" 0.5 , 1.0 ")
    assert result == pytest.approx([0.5, 1.0])


def test_parse_thresholds_bad_data_returns_default():
    result = _parse_thresholds("bad,data")
    assert result == pytest.approx([0.5, 0.8, 1.0])


def test_parse_thresholds_single_value():
    assert _parse_thresholds("0.75") == pytest.approx([0.75])


# ── _model_token_budget ───────────────────────────────────────────────────────


def test_model_token_budget_sonnet():
    s = MagicMock()
    s.sonnet_daily_token_budget = 500_000
    s.haiku_daily_token_budget = 1_000_000
    assert CostTracker._model_token_budget(s, "claude-sonnet-4-6") == 500_000


def test_model_token_budget_haiku():
    s = MagicMock()
    s.sonnet_daily_token_budget = 500_000
    s.haiku_daily_token_budget = 1_000_000
    assert CostTracker._model_token_budget(s, "claude-haiku-4-5-20251001") == 1_000_000


def test_model_token_budget_unknown_is_zero():
    s = MagicMock()
    assert CostTracker._model_token_budget(s, "gpt-4o") == 0


# ── check_budget (mocked Redis) ───────────────────────────────────────────────


def _mock_settings(ceiling: float = 10.0, sonnet_budget: int = 0, haiku_budget: int = 0):
    s = MagicMock()
    s.daily_cost_ceiling_usd = ceiling
    s.sonnet_daily_token_budget = sonnet_budget
    s.haiku_daily_token_budget = haiku_budget
    return s


def test_check_budget_raises_when_ceiling_hit():
    tracker = CostTracker()
    mock_redis = MagicMock()
    mock_redis.get.return_value = "10.50"  # over the $10 ceiling
    tracker._redis = mock_redis

    with patch("app.brain.cost_tracker.get_settings", return_value=_mock_settings(ceiling=10.0)):
        with pytest.raises(BudgetExceeded, match="ceiling"):
            tracker.check_budget("claude-sonnet-4-6")


def test_check_budget_passes_when_under_ceiling():
    tracker = CostTracker()
    mock_redis = MagicMock()
    mock_redis.get.return_value = "1.00"
    tracker._redis = mock_redis

    with patch("app.brain.cost_tracker.get_settings", return_value=_mock_settings(ceiling=10.0)):
        tracker.check_budget("claude-sonnet-4-6")  # must not raise


def test_check_budget_unlimited_when_ceiling_is_zero():
    """ceiling=0 means unlimited — Redis should not be queried for the total."""
    tracker = CostTracker()
    mock_redis = MagicMock()
    tracker._redis = mock_redis

    with patch("app.brain.cost_tracker.get_settings", return_value=_mock_settings(ceiling=0.0)):
        tracker.check_budget("claude-sonnet-4-6")  # must not raise

    mock_redis.get.assert_not_called()


def test_check_budget_raises_when_model_token_budget_exhausted():
    tracker = CostTracker()
    mock_redis = MagicMock()
    # Under cost ceiling, but token counts exceed budget
    mock_redis.get.side_effect = ["1.00", "400000", "200000"]  # total, in, out
    tracker._redis = mock_redis

    with patch(
        "app.brain.cost_tracker.get_settings",
        return_value=_mock_settings(ceiling=10.0, sonnet_budget=500_000),
    ):
        with pytest.raises(BudgetExceeded, match="token budget"):
            tracker.check_budget("claude-sonnet-4-6")
