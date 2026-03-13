"""Tests for agentic tool schemas and _tool_executor routing."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_agentic_tools_all_have_required_schema_keys():
    from app.brain.llm_router import AGENTIC_TOOLS

    for tool in AGENTIC_TOOLS:
        assert "name" in tool, f"Tool missing 'name': {tool}"
        assert "description" in tool, f"Tool '{tool.get('name')}' missing 'description'"
        assert "input_schema" in tool, f"Tool '{tool.get('name')}' missing 'input_schema'"
        assert tool["input_schema"].get("type") == "object", (
            f"Tool '{tool['name']}' input_schema.type must be 'object'"
        )


def test_agentic_tools_includes_new_tools():
    from app.brain.llm_router import AGENTIC_TOOLS

    names = {t["name"] for t in AGENTIC_TOOLS}
    expected = {"github_read", "github_write", "cicd_read", "cicd_trigger", "rmm_read", "deploy", "compound_plan"}
    assert expected.issubset(names), f"Missing tools: {expected - names}"


def test_tool_executor_routes_via_skill_registry():
    """_tool_executor routes known tools to skill.execute()."""
    mock_skill = MagicMock()
    mock_skill.is_available.return_value = True
    mock_skill.__class__ = type("GmailReadSkill", (), {})
    mock_result = MagicMock()
    mock_result.context_data = "inbox data"
    mock_skill.execute = AsyncMock(return_value=mock_result)

    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_skill

    import asyncio
    from app.brain.dispatcher import Dispatcher

    # _tool_executor only needs self.skills — construct minimal Dispatcher
    d = Dispatcher.__new__(Dispatcher)
    d.skills = mock_registry
    result = asyncio.run(d._tool_executor("gmail_read", {}))

    assert result == "inbox data"


def test_tool_executor_unknown_skill_returns_error_string():
    """_tool_executor returns error string for unknown tool names."""
    from app.skills.chat_skill import ChatSkill

    chat_skill = ChatSkill()
    mock_registry = MagicMock()
    mock_registry.get.return_value = chat_skill

    import asyncio
    from app.brain.dispatcher import Dispatcher

    d = Dispatcher.__new__(Dispatcher)
    d.skills = mock_registry
    result = asyncio.run(d._tool_executor("nonexistent_tool", {}))

    assert "[Unknown tool:" in result


def test_tool_executor_unavailable_skill_returns_config_note():
    """_tool_executor returns config note when skill.is_available() is False."""
    mock_skill = MagicMock()
    mock_skill.is_available.return_value = False
    mock_skill.__class__ = type("IONOSCloudSkill", (), {})

    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_skill

    import asyncio
    from app.brain.dispatcher import Dispatcher

    d = Dispatcher.__new__(Dispatcher)
    d.skills = mock_registry
    result = asyncio.run(d._tool_executor("ionos_cloud", {}))

    assert "not configured" in result


def test_tool_executor_skill_exception_returns_error_string():
    """_tool_executor returns error string when skill.execute() raises."""
    mock_skill = MagicMock()
    mock_skill.is_available.return_value = True
    mock_skill.__class__ = type("GmailReadSkill", (), {})
    mock_skill.execute = AsyncMock(side_effect=RuntimeError("API timeout"))

    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_skill

    import asyncio
    from app.brain.dispatcher import Dispatcher

    d = Dispatcher.__new__(Dispatcher)
    d.skills = mock_registry
    result = asyncio.run(d._tool_executor("gmail_read", {}))

    assert "[Tool error:" in result
