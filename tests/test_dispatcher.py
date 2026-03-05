"""Unit tests for Dispatcher constants and DispatchResult dataclass."""

import pytest

from app.brain.dispatcher import (
    DispatchResult,
    PENDING_TTL,
    _CANCEL_WORDS,
    _CONFIRM_WORDS,
)


# ── DispatchResult dataclass ──────────────────────────────────────────────────


def test_dispatch_result_minimal_construction():
    result = DispatchResult(reply="Hello", intent="chat", session_id="s1")
    assert result.reply == "Hello"
    assert result.intent == "chat"
    assert result.session_id == "s1"


def test_dispatch_result_default_agent():
    result = DispatchResult(reply="Hi", intent="chat", session_id="s2")
    assert result.agent == "default"


def test_dispatch_result_custom_agent():
    result = DispatchResult(reply="Done", intent="gmail_read", session_id="s3", agent="gmail_bot")
    assert result.agent == "gmail_bot"


def test_dispatch_result_is_dataclass():
    """DispatchResult should support equality comparison like a dataclass."""
    r1 = DispatchResult(reply="x", intent="chat", session_id="s", agent="default")
    r2 = DispatchResult(reply="x", intent="chat", session_id="s", agent="default")
    assert r1 == r2


# ── Confirmation trigger words ────────────────────────────────────────────────


def test_confirm_words_covers_key_triggers():
    for word in ("yes", "confirm", "proceed", "go ahead", "send it"):
        assert word in _CONFIRM_WORDS, f"'{word}' should be in _CONFIRM_WORDS"


def test_cancel_words_covers_key_triggers():
    for word in ("no", "cancel", "abort", "stop"):
        assert word in _CANCEL_WORDS, f"'{word}' should be in _CANCEL_WORDS"


def test_confirm_and_cancel_sets_are_disjoint():
    overlap = _CONFIRM_WORDS & _CANCEL_WORDS
    assert not overlap, f"These words appear in both sets: {overlap}"


def test_confirm_words_are_lowercase():
    for word in _CONFIRM_WORDS:
        assert word == word.lower(), f"'{word}' in _CONFIRM_WORDS should be lowercase"


def test_cancel_words_are_lowercase():
    for word in _CANCEL_WORDS:
        assert word == word.lower(), f"'{word}' in _CANCEL_WORDS should be lowercase"


# ── PENDING_TTL sanity check ──────────────────────────────────────────────────


def test_pending_ttl_is_reasonable():
    """Pending actions should time out between 1 minute and 1 hour."""
    assert 60 <= PENDING_TTL <= 3_600, f"PENDING_TTL={PENDING_TTL} is outside [60, 3600]"
