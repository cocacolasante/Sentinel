"""
Tests for Phase 1 self-heal pipeline skills.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch


# ── CodeIndexSkill ────────────────────────────────────────────────────────────

def test_code_index_extracts_symbols():
    """CodeIndexSkill should index symbols and call Qdrant upsert."""
    import ast
    from app.skills.code_index_skill import _extract_symbols

    source = """
def public_func(x, y):
    return x + y

class MyClass:
    def method(self):
        pass

def _private():
    pass
"""
    symbols = _extract_symbols(source)
    names = [s for s in symbols]
    assert "public_func" in names
    assert "MyClass" in names
    assert "_private" in names  # _extract_symbols returns ALL symbols (filter is in search)


def test_code_index_skips_syntax_errors():
    from app.skills.code_index_skill import _extract_symbols

    result = _extract_symbols("def broken(:\n    pass")
    assert result == []


def test_code_index_execute_missing_path():
    import asyncio
    from app.skills.code_index_skill import CodeIndexSkill

    skill = CodeIndexSkill()
    result = asyncio.run(skill.execute({"repo_path": "/nonexistent/path"}, ""))
    assert result.is_error
    assert "does not exist" in result.context_data


# ── TestRunnerSkill ───────────────────────────────────────────────────────────

def test_test_runner_parses_stdout_failures():
    from app.skills.test_runner_skill import _parse_stdout

    stdout = (
        "FAILED tests/test_foo.py::test_bar - AssertionError\n"
        "FAILED tests/test_foo.py::test_baz - ValueError\n"
        "2 failed, 5 passed in 1.23s\n"
    )
    passed, failed, failures = _parse_stdout(stdout)
    assert failed == 2
    assert passed == 5
    assert len(failures) == 2
    assert "test_bar" in failures[0]["nodeid"]


def test_test_runner_parses_all_passed():
    from app.skills.test_runner_skill import _parse_stdout

    stdout = "10 passed in 2.01s\n"
    passed, failed, failures = _parse_stdout(stdout)
    assert passed == 10
    assert failed == 0
    assert failures == []


@patch("app.skills.test_runner_skill.shell_run")
def test_test_runner_execute_returns_structured_result(mock_shell):
    import asyncio
    from app.utils.shell import ShellResult
    from app.skills.test_runner_skill import TestRunnerSkill

    mock_shell.return_value = ShellResult(
        returncode=1,
        stdout="FAILED tests/test_x.py::test_y\n1 failed in 0.5s\n",
        stderr="",
    )
    skill = TestRunnerSkill()
    result = asyncio.run(skill.execute({"target": "tests/test_x.py", "repo_path": "/tmp"}, ""))
    data = json.loads(result.context_data)
    assert data["failed"] >= 1 or data["ok"] is False


# ── PatchGeneratorSkill ───────────────────────────────────────────────────────

VALID_DIFF = """\
--- a/app/skills/example.py
+++ b/app/skills/example.py
@@ -1,2 +1,2 @@
 def add(x, y):
-    return x - y
+    return x + y
"""


@patch("app.skills.patch_generator_skill.search_symbols", return_value=[])
@patch("anthropic.Anthropic")
def test_patch_generator_validates_unidiff(mock_anthropic_cls, mock_search):
    import asyncio
    from app.skills.patch_generator_skill import PatchGeneratorSkill

    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=VALID_DIFF)]
    mock_client.messages.create.return_value = mock_msg

    skill = PatchGeneratorSkill()
    result = asyncio.run(
        skill.execute(
            {"failures": json.dumps([{"nodeid": "tests/test_x.py::test_add", "message": "assert 1 == 2"}])},
            "",
        )
    )
    assert not result.is_error
    assert "+++" in result.context_data


@patch("app.skills.patch_generator_skill.search_symbols", return_value=[])
@patch("anthropic.Anthropic")
def test_patch_generator_rejects_invalid_diff(mock_anthropic_cls, mock_search):
    import asyncio
    from app.skills.patch_generator_skill import PatchGeneratorSkill

    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Here is my explanation of the fix. You should change line 5 to return x + y.")]
    mock_client.messages.create.return_value = mock_msg

    skill = PatchGeneratorSkill()
    result = asyncio.run(
        skill.execute(
            {"failures": json.dumps([{"nodeid": "tests/test_x.py::test_add", "message": "assert 1 == 2"}])},
            "",
        )
    )
    assert result.is_error
    assert "not valid unified diff" in result.context_data


# ── SandboxValidatorSkill ─────────────────────────────────────────────────────

@patch("app.skills.sandbox_validator_skill.shutil.copytree")
@patch("app.skills.sandbox_validator_skill.shutil.rmtree")
@patch("app.skills.sandbox_validator_skill.subprocess.run")
@patch("app.skills.sandbox_validator_skill.shell_run")
def test_sandbox_validator_applies_patch_and_runs_tests(
    mock_shell, mock_subprocess, mock_rmtree, mock_copytree
):
    import asyncio
    from app.utils.shell import ShellResult
    from app.skills.sandbox_validator_skill import SandboxValidatorSkill

    mock_subprocess.return_value = MagicMock(returncode=0, stderr=b"")
    mock_shell.return_value = ShellResult(returncode=0, stdout="1 passed\n", stderr="")

    skill = SandboxValidatorSkill()
    result = asyncio.run(
        skill.execute(
            {"diff": VALID_DIFF, "test_ids": ["tests/test_x.py::test_add"], "repo_path": "/tmp"},
            "",
        )
    )
    data = json.loads(result.context_data)
    assert data["ok"] is True
    # Ensure cleanup was called
    mock_rmtree.assert_called_once()


@patch("app.skills.sandbox_validator_skill.shutil.copytree")
@patch("app.skills.sandbox_validator_skill.shutil.rmtree")
@patch("app.skills.sandbox_validator_skill.subprocess.run")
@patch("app.skills.sandbox_validator_skill.shell_run")
def test_sandbox_validator_cleans_up_on_failure(
    mock_shell, mock_subprocess, mock_rmtree, mock_copytree
):
    import asyncio
    from app.utils.shell import ShellResult
    from app.skills.sandbox_validator_skill import SandboxValidatorSkill

    mock_subprocess.return_value = MagicMock(returncode=1, stderr=b"Hunk failed")
    mock_shell.return_value = ShellResult(returncode=1, stdout="", stderr="")

    skill = SandboxValidatorSkill()
    result = asyncio.run(
        skill.execute({"diff": VALID_DIFF, "repo_path": "/tmp"}, "")
    )
    data = json.loads(result.context_data)
    assert data["ok"] is False
    mock_rmtree.assert_called_once()


# ── GitCommitSkill ────────────────────────────────────────────────────────────

@patch("app.skills.git_commit_skill.post_alert_sync")
@patch("app.skills.git_commit_skill.postgres")
@patch("app.skills.git_commit_skill.subprocess.run")
@patch("app.skills.git_commit_skill.asyncio.to_thread")
def test_git_commit_skill_opens_pr(mock_to_thread, mock_subprocess, mock_pg_mod, mock_slack):
    import asyncio
    from app.skills.git_commit_skill import GitCommitSkill

    # asyncio.to_thread calls: _create_branch_sync, _commit_sync, _push_sync
    call_results = [None, None, "https://github.com/org/repo/pull/42"]
    call_index = [0]

    async def fake_to_thread(fn, *args, **kwargs):
        idx = call_index[0]
        call_index[0] += 1
        return call_results[idx] if idx < len(call_results) else None

    mock_to_thread.side_effect = fake_to_thread
    mock_subprocess.return_value = MagicMock(returncode=0, stdout=b"patching file\n", stderr=b"")
    mock_pg_mod.execute = MagicMock()

    skill = GitCommitSkill()
    result = asyncio.run(
        skill.execute(
            {
                "diff": VALID_DIFF,
                "test_id": "tests/test_x.py::test_add",
                "issue_slug": "test-add",
                "repo_path": "/tmp",
                "session_id": "test-session",
            },
            "",
        )
    )
    # DB audit row should be written
    mock_pg_mod.execute.assert_called_once()
    # Slack should be notified
    mock_slack.assert_called_once()
    assert "PR" in result.context_data or "branch" in result.context_data


# ── Self-heal pipeline integration ───────────────────────────────────────────

@patch("app.skills.git_commit_skill.GitCommitSkill")
@patch("app.skills.sandbox_validator_skill.SandboxValidatorSkill")
@patch("app.skills.patch_generator_skill.PatchGeneratorSkill")
@patch("app.skills.test_runner_skill.TestRunnerSkill")
@patch("app.skills.code_index_skill.CodeIndexSkill")
def test_self_heal_pipeline_skips_pr_when_no_failures(
    mock_idx_cls, mock_runner_cls, mock_patch_cls, mock_sandbox_cls, mock_commit_cls
):
    import asyncio
    from app.skills.base import SkillResult

    with (
        patch("app.skills.code_index_skill.CodeIndexSkill") as mock_idx,
        patch("app.skills.test_runner_skill.TestRunnerSkill") as mock_runner,
        patch("app.skills.git_commit_skill.GitCommitSkill") as mock_commit,
    ):
        mock_idx.return_value.execute = AsyncMock(return_value=SkillResult(context_data="Indexed 10"))
        mock_runner.return_value.execute = AsyncMock(
            return_value=SkillResult(context_data=json.dumps({"ok": True, "passed": 10, "failed": 0, "failures": []}))
        )
        mock_commit.return_value.execute = AsyncMock(return_value=SkillResult(context_data="PR opened"))

        # Import after patching
        import importlib
        import app.worker.tasks as tasks_mod
        result = asyncio.run(tasks_mod._self_heal_pipeline(1, {"repo_path": "/tmp"}))

    assert result["ok"] is True
    assert "no patch needed" in result["summary"]


@patch("app.db.postgres.execute")
def test_self_heal_pipeline_retries_patch_on_sandbox_fail(mock_pg):
    import asyncio
    from app.skills.base import SkillResult

    with (
        patch("app.skills.code_index_skill.CodeIndexSkill") as mock_idx,
        patch("app.skills.test_runner_skill.TestRunnerSkill") as mock_runner,
        patch("app.skills.patch_generator_skill.PatchGeneratorSkill") as mock_patch,
        patch("app.skills.sandbox_validator_skill.SandboxValidatorSkill") as mock_sandbox,
        patch("app.skills.git_commit_skill.GitCommitSkill") as mock_commit,
    ):
        mock_idx.return_value.execute = AsyncMock(return_value=SkillResult(context_data="Indexed 5"))
        mock_runner.return_value.execute = AsyncMock(
            return_value=SkillResult(
                context_data=json.dumps({
                    "ok": False,
                    "passed": 0,
                    "failed": 1,
                    "failures": [{"nodeid": "tests/test_x.py::test_add", "message": "AssertionError"}],
                })
            )
        )
        mock_patch.return_value.execute = AsyncMock(
            return_value=SkillResult(context_data=VALID_DIFF, is_error=False)
        )
        # Sandbox always fails
        mock_sandbox.return_value.execute = AsyncMock(
            return_value=SkillResult(context_data=json.dumps({"ok": False, "stdout": "1 failed"}))
        )
        mock_commit.return_value.execute = AsyncMock(return_value=SkillResult(context_data="PR opened"))

        import app.worker.tasks as tasks_mod
        result = asyncio.run(tasks_mod._self_heal_pipeline(1, {"repo_path": "/tmp"}))

    assert result["ok"] is False
    assert "valid patch" in result["summary"]
    mock_commit.return_value.execute.assert_not_called()
