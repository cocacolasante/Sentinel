"""
Unit tests for SEWorkflowSkill.

No real LLM calls or git operations — all external I/O is mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.skills.se_workflow_skill import (
    SEWorkflowSkill,
    _PROJECTS_DIR,
    _SE_TASKS_DIR,
    _SENTINEL_WORKSPACE,
    _resolve_dirs,
    _slugify,
)
from app.skills.base import SkillResult


# ── Helpers ───────────────────────────────────────────────────────────────────


def test_slugify_special_chars():
    assert _slugify("Hello, World! This is a Test.") == "hello-world-this-is-a-test"


def test_slugify_length_cap():
    long_title = "a" * 100
    result = _slugify(long_title)
    assert len(result) <= 60


def test_slugify_strips_leading_trailing_dashes():
    assert _slugify("---foo bar---") == "foo-bar"


# ── Instantiation ─────────────────────────────────────────────────────────────


def test_skill_instantiation():
    skill = SEWorkflowSkill()
    assert skill.name == "se_workflow"
    expected_intents = {
        "se_brainstorm",
        "se_spec",
        "se_plan",
        "se_implement",
        "se_review",
        "se_workflow",
        "se_new_project",
        "se_status",
    }
    assert set(skill.trigger_intents) == expected_intents
    assert len(skill.trigger_intents) == 8


# ── Path constants ────────────────────────────────────────────────────────────


def test_path_constants():
    assert _SENTINEL_WORKSPACE == "/root/sentinel-workspace"
    assert _SE_TASKS_DIR == "/root/sentinel-workspace/se-tasks"
    assert _PROJECTS_DIR == "/root/projects"


# ── _resolve_dirs ─────────────────────────────────────────────────────────────


def test_resolve_dirs_sentinel_type():
    task_dir, git_cwd = _resolve_dirs("my-feature", "sentinel")
    assert task_dir == "/root/sentinel-workspace/se-tasks/my-feature"
    assert git_cwd == "/root/sentinel-workspace"


def test_resolve_dirs_project_type():
    task_dir, git_cwd = _resolve_dirs("my-client-site", "project")
    assert task_dir == "/root/projects/my-client-site"
    assert git_cwd == "/root/projects/my-client-site"


# ── se_status ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_se_status_no_rows():
    skill = SEWorkflowSkill()
    with patch("app.skills.se_workflow_skill._query_tasks", return_value=[]):
        result = await skill.execute({"intent": "se_status"}, "se status")
    assert isinstance(result, SkillResult)
    assert not result.is_error
    assert "No SE tasks" in result.context_data


@pytest.mark.asyncio
async def test_execute_se_status_with_rows():
    skill = SEWorkflowSkill()
    mock_rows = [
        {"id": 1, "slug": "test-feature", "title": "Test Feature", "phase": "plan", "status": "done", "project_type": "sentinel"},
    ]
    with patch("app.skills.se_workflow_skill._query_tasks", return_value=mock_rows):
        result = await skill.execute({"intent": "se_status"}, "se status")
    assert isinstance(result, SkillResult)
    assert "Test Feature" in result.context_data


# ── Phase brainstorm ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_brainstorm_writes_files(tmp_path):
    skill = SEWorkflowSkill()
    task_dir = str(tmp_path)

    llm_response = (
        "1. Idea one\n2. Idea two\n--- SPRINT ---\n"
        "Story 1: As a user I want X\nStory 2: As a user I want Y"
    )
    with patch.object(skill, "_llm", new=AsyncMock(return_value=llm_response)):
        await skill._phase_brainstorm(
            task_dir=task_dir,
            slug="test-slug",
            title="Test Feature",
            description="A description",
            repo="",
            project_type="sentinel",
        )

    brainstorm_path = tmp_path / "brainstorm.md"
    sprint_path = tmp_path / "sprint.md"
    assert brainstorm_path.exists(), "brainstorm.md should be created"
    assert sprint_path.exists(), "sprint.md should be created"
    assert "Idea one" in brainstorm_path.read_text()
    assert "Story 1" in sprint_path.read_text()


# ── git commit failure is non-fatal ──────────────────────────────────────────


def test_git_commit_failure_is_nonfatal():
    """_git_commit must not raise even if subprocess fails."""
    from app.skills.se_workflow_skill import _git_commit

    with patch("subprocess.run", side_effect=OSError("git not found")):
        # Should not raise
        _git_commit("/tmp/fake-task", "/tmp/fake-repo", "test-slug", "brainstorm")
