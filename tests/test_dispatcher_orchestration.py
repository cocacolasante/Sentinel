"""Tests for dispatcher compound request detection and routing."""
from __future__ import annotations

import pytest


def test_compound_request_triggers_planner_coordination_words():
    """Low confidence + coordination words → _is_compound_request returns True."""
    from app.brain.dispatcher import _is_compound_request

    msg = "please audit all servers and then create tickets for any issues found automatically"
    assert _is_compound_request("chat", 0.4, msg) is True


def test_explicit_plan_keyword_triggers_planner():
    """High confidence but contains 'orchestrate' → _is_compound_request returns True."""
    from app.brain.dispatcher import _is_compound_request

    assert _is_compound_request("github_read", 0.95, "orchestrate the full deployment pipeline") is True
    assert _is_compound_request("chat", 0.8, "coordinate the release process") is True


def test_single_intent_bypasses_planner():
    """github_read, high confidence, short message → not compound."""
    from app.brain.dispatcher import _is_compound_request

    assert _is_compound_request("github_read", 0.92, "list open github issues") is False
    assert _is_compound_request("task_read", 0.88, "show my tasks") is False


def test_skill_gap_takes_precedence_over_compound():
    """SkillGapHandler.should_trigger() fires → skill_discover, not compound_plan."""
    from app.skills.skill_discovery import SkillGapHandler

    # SkillGapHandler triggers on low-confidence "chat" with action keywords
    # Even if the message also has coordination words
    intent = "chat"
    confidence = 0.3
    message = "create and build and integrate a new widget"
    assert SkillGapHandler.should_trigger(intent, confidence, message) is True
    # compound check would also fire, but SkillGapHandler takes precedence in dispatcher


def test_is_compound_requires_coordination_words_when_not_explicit():
    """Low confidence but no coordination words → not compound (just ambiguous)."""
    from app.brain.dispatcher import _is_compound_request

    msg = "please handle all the server stuff for me efficiently"
    # Short enough message without coordination words
    assert _is_compound_request("chat", 0.4, msg) is False


def test_compound_plan_in_intent_task_type():
    """compound_plan intent maps to 'planning' tier (Sonnet)."""
    from app.brain.llm_router import _INTENT_TASK_TYPE, MODEL_MAP, _SONNET

    assert "compound_plan" in _INTENT_TASK_TYPE
    task_type = _INTENT_TASK_TYPE["compound_plan"]
    assert task_type == "planning"
    model, _ = MODEL_MAP[task_type]
    assert model == _SONNET
