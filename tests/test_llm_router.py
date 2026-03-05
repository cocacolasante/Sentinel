"""Unit tests for LLMRouter — model selection and prompt building."""

import pytest
from unittest.mock import patch

from app.brain.llm_router import DEFAULT_AGENT_PROMPT, MODEL_MAP, LLMRouter


# ── MODEL_MAP structure ───────────────────────────────────────────────────────


def test_model_map_contains_all_required_task_types():
    required = {"code", "reasoning", "writing", "research", "classify", "default"}
    assert required.issubset(set(MODEL_MAP.keys()))


def test_model_map_values_are_valid_tuples():
    for task_type, (model_id, max_tokens) in MODEL_MAP.items():
        assert isinstance(model_id, str) and len(model_id) > 0, f"{task_type}: model_id must be a non-empty string"
        assert isinstance(max_tokens, int) and max_tokens > 0, f"{task_type}: max_tokens must be a positive int"


def test_model_map_classify_uses_haiku():
    """Fast intent classification should use the cheaper Haiku model."""
    model_id, _ = MODEL_MAP["classify"]
    assert "haiku" in model_id.lower()


def test_model_map_code_has_large_token_budget():
    """Code generation needs more tokens than the default."""
    _, code_tokens = MODEL_MAP["code"]
    _, default_tokens = MODEL_MAP["default"]
    assert code_tokens >= default_tokens


# ── _select_model ─────────────────────────────────────────────────────────────


def test_select_model_returns_correct_entry_for_each_type():
    router = LLMRouter()
    for task_type, expected in MODEL_MAP.items():
        assert router._select_model(task_type) == expected


def test_select_model_unknown_type_falls_back_to_default():
    router = LLMRouter()
    result = router._select_model("completely_unknown_task_xyz")
    assert result == MODEL_MAP["default"]


def test_select_model_classify_returns_haiku():
    router = LLMRouter()
    model, _ = router._select_model("classify")
    assert "haiku" in model.lower()


# ── DEFAULT_AGENT_PROMPT content ─────────────────────────────────────────────


def test_default_prompt_contains_shell_command_restriction():
    assert "NEVER output shell commands" in DEFAULT_AGENT_PROMPT


def test_default_prompt_references_workspace_path():
    assert "sentinel-workspace" in DEFAULT_AGENT_PROMPT


def test_default_prompt_contains_safe_code_change_workflow():
    assert "feat/" in DEFAULT_AGENT_PROMPT or "feature branch" in DEFAULT_AGENT_PROMPT.lower()


def test_default_prompt_forbids_push_to_main():
    assert "main" in DEFAULT_AGENT_PROMPT
    assert "NEVER" in DEFAULT_AGENT_PROMPT


def test_default_prompt_contains_env_protection():
    assert ".env" in DEFAULT_AGENT_PROMPT


# ── _build_system_prompt ──────────────────────────────────────────────────────


def test_build_system_prompt_no_agent_no_telos():
    from app.brain.llm_router import _telos_loader

    router = LLMRouter()
    with patch.object(_telos_loader, "get_block", return_value=""):
        prompt = router._build_system_prompt(agent=None)
    assert prompt == DEFAULT_AGENT_PROMPT


def test_build_system_prompt_appends_telos_block():
    from app.brain.llm_router import _telos_loader

    router = LLMRouter()
    telos_content = "## Anthony's Goals\n- Ship great software"
    with patch.object(_telos_loader, "get_block", return_value=telos_content):
        prompt = router._build_system_prompt(agent=None)
    assert DEFAULT_AGENT_PROMPT in prompt
    assert telos_content in prompt


def test_build_system_prompt_telos_comes_after_agent_prompt():
    from app.brain.llm_router import _telos_loader

    router = LLMRouter()
    telos_content = "## TELOS_MARKER"
    with patch.object(_telos_loader, "get_block", return_value=telos_content):
        prompt = router._build_system_prompt(agent=None)
    agent_pos = prompt.find(DEFAULT_AGENT_PROMPT[:50])
    telos_pos = prompt.find("TELOS_MARKER")
    assert agent_pos < telos_pos
