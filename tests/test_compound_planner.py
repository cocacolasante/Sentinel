"""Tests for CompoundPlannerSkill and _is_compound_request helper."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch


def test_is_compound_request_detects_coordination_words():
    from app.brain.dispatcher import _is_compound_request

    msg = "please audit all servers and then create tickets for any issues found automatically"
    assert _is_compound_request("chat", 0.4, msg) is True


def test_is_compound_request_detects_explicit_plan_keyword():
    from app.brain.dispatcher import _is_compound_request

    assert _is_compound_request("github_read", 0.9, "orchestrate the full deployment") is True
    assert _is_compound_request("chat", 0.9, "plan the migration strategy") is True


def test_single_step_not_compound():
    from app.brain.dispatcher import _is_compound_request

    assert _is_compound_request("github_read", 0.9, "list open github issues") is False
    assert _is_compound_request("chat", 0.9, "what is the weather") is False


def test_low_confidence_short_message_not_compound():
    from app.brain.dispatcher import _is_compound_request

    # Low confidence but short message — not compound
    assert _is_compound_request("chat", 0.3, "fix the bug") is False


def test_compound_planner_creates_task_dag():
    """CompoundPlannerSkill creates tasks and sets blocked_by chains."""
    plan_json = json.dumps({
        "plan_title": "Audit and Fix",
        "tasks": [
            {"title": "Audit servers", "description": "Check all servers", "skill_hint": "rmm_read", "commands": [], "priority": 3},
            {"title": "Fix issues", "description": "Fix found issues", "skill_hint": "server_shell", "commands": [], "priority": 3},
        ],
        "dependencies": [[1, 2]],
    })

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=plan_json)]

    mock_tc_result = MagicMock()
    mock_tc_result.context_data = "Task ID: #42"

    mock_reg = MagicMock()
    mock_reg.list_available.return_value = []

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp

    mock_tc = MagicMock()
    mock_tc.execute = AsyncMock(return_value=mock_tc_result)

    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.brain.dispatcher._build_skill_registry", return_value=mock_reg), \
         patch("app.skills.task_skill.TaskCreateSkill", return_value=mock_tc), \
         patch("app.db.postgres.execute"), \
         patch("app.db.postgres.execute_one"), \
         patch("app.config.get_settings") as mock_settings:

        mock_settings.return_value.anthropic_api_key = "test-key"
        mock_settings.return_value.model_sonnet = "claude-sonnet-4-6"

        import asyncio
        from app.skills.compound_planner import CompoundPlannerSkill

        skill = CompoundPlannerSkill()
        result = asyncio.run(skill.execute({"session_id": "test"}, "audit servers and then fix issues"))

    assert not result.is_error
    assert "Audit and Fix" in result.context_data
    assert mock_tc.execute.call_count == 2


def test_compound_planner_sets_blocked_by_chains():
    """CompoundPlannerSkill calls UPDATE tasks SET blocked_by for dependent tasks."""
    plan_json = json.dumps({
        "plan_title": "Chain Plan",
        "tasks": [
            {"title": "Step 1", "description": "", "skill_hint": "", "commands": [], "priority": 3},
            {"title": "Step 2", "description": "", "skill_hint": "", "commands": [], "priority": 3},
        ],
        "dependencies": [[1, 2]],
    })

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=plan_json)]

    call_count = [0]

    async def mock_execute(params, msg):
        call_count[0] += 1
        r = MagicMock()
        r.context_data = f"Task ID: #{call_count[0] + 100}"
        return r

    mock_reg = MagicMock()
    mock_reg.list_available.return_value = []

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp

    pg_execute = MagicMock()

    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.brain.dispatcher._build_skill_registry", return_value=mock_reg), \
         patch("app.skills.task_skill.TaskCreateSkill", return_value=MagicMock(execute=mock_execute)), \
         patch("app.db.postgres.execute", pg_execute), \
         patch("app.db.postgres.execute_one"), \
         patch("app.config.get_settings") as mock_settings:

        mock_settings.return_value.anthropic_api_key = "test-key"
        mock_settings.return_value.model_sonnet = "claude-sonnet-4-6"

        import asyncio
        from app.skills.compound_planner import CompoundPlannerSkill

        skill = CompoundPlannerSkill()
        asyncio.run(skill.execute({"session_id": "test"}, "step 1 and then step 2"))

    # blocked_by UPDATE should have been called
    update_calls = [c for c in pg_execute.call_args_list if "blocked_by" in str(c)]
    assert len(update_calls) >= 1


def test_compound_planner_handles_llm_json_parse_failure():
    """CompoundPlannerSkill returns is_error=True when LLM returns invalid JSON."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="this is not json")]

    mock_reg = MagicMock()
    mock_reg.list_available.return_value = []

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp

    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.brain.dispatcher._build_skill_registry", return_value=mock_reg), \
         patch("app.config.get_settings") as mock_settings:

        mock_settings.return_value.anthropic_api_key = "test-key"
        mock_settings.return_value.model_sonnet = "claude-sonnet-4-6"

        import asyncio
        from app.skills.compound_planner import CompoundPlannerSkill

        skill = CompoundPlannerSkill()
        result = asyncio.run(skill.execute({}, "do everything"))

    assert result.is_error
    assert "Could not decompose plan" in result.context_data
