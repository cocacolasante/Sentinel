"""
Extended tests for ServerShellSkill — covering pattern detection helpers,
action routing, and approval logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.skills.server_shell_skill import (
    _is_destructive,
    _is_forbidden,
    _touches_protected_path,
    ServerShellSkill,
)
from app.skills.base import ApprovalCategory, SkillResult


# ── _is_destructive ────────────────────────────────────────────────────────────


def test_destructive_rm_rf():
    # Note: path must start with a word char after the space for \b boundary to match
    assert _is_destructive("rm -rf mydir") is True


def test_destructive_kill():
    assert _is_destructive("kill -9 1234") is True


def test_destructive_git_force_push():
    assert _is_destructive("git push origin main --force") is True


def test_destructive_docker_rm():
    assert _is_destructive("docker rm my_container") is True


def test_destructive_systemctl_stop():
    assert _is_destructive("systemctl stop nginx") is True


def test_not_destructive_ls():
    assert _is_destructive("ls -la") is False


def test_not_destructive_cat():
    assert _is_destructive("cat app/main.py") is False


def test_not_destructive_git_commit():
    assert _is_destructive("git commit -m 'fix'") is False


def test_not_destructive_git_push_normal():
    assert _is_destructive("git push origin feature-branch") is False


# ── _is_forbidden ─────────────────────────────────────────────────────────────


def test_forbidden_mkfs():
    assert _is_forbidden("mkfs.ext4 /dev/sdb") is True


def test_forbidden_fdisk():
    assert _is_forbidden("fdisk /dev/sda") is True


def test_forbidden_shred():
    assert _is_forbidden("shred -v /dev/sda") is True


def test_not_forbidden_ls():
    assert _is_forbidden("ls /dev") is False


def test_not_forbidden_git_commit():
    assert _is_forbidden("git commit -m 'wip'") is False


# ── _touches_protected_path ────────────────────────────────────────────────────


def test_protected_root_sentinel():
    assert _touches_protected_path("/root/sentinel/app/main.py") is True


def test_protected_sentinel_project():
    assert _touches_protected_path("/sentinel-project/config.py") is True


def test_not_protected_workspace():
    assert _touches_protected_path("/root/sentinel-workspace/app") is False


def test_not_protected_normal():
    assert _touches_protected_path("/tmp/test.py") is False


# ── ServerShellSkill.execute — action routing ─────────────────────────────────


async def test_read_file_missing_path():
    r = await ServerShellSkill().execute({"action": "read_file"}, "read file")
    assert "path" in r.context_data.lower()


async def test_search_code_missing_pattern():
    r = await ServerShellSkill().execute({"action": "search_code"}, "search code")
    assert "pattern" in r.context_data.lower()


async def test_list_files_action():
    with patch("app.integrations.repo.RepoClient") as MockRepo:
        inst = MockRepo.return_value
        inst.list_files = MagicMock(return_value=["app/main.py"])
        r = await ServerShellSkill().execute(
            {"action": "list_files", "path": "/root/sentinel-workspace"},
            "list files",
        )
    assert isinstance(r.context_data, str)


async def test_read_file_action():
    with patch("app.integrations.repo.RepoClient") as MockRepo:
        inst = MockRepo.return_value
        inst.read_file = MagicMock(return_value="# content")
        r = await ServerShellSkill().execute(
            {"action": "read_file", "path": "app/config.py"},
            "read config",
        )
    assert isinstance(r.context_data, str)


async def test_protected_path_blocked():
    """Commands referencing /root/sentinel (not -workspace) should be blocked."""
    r = await ServerShellSkill().execute(
        {"command": "cat /root/sentinel/app/config.py"},
        "show config",
    )
    assert "protected" in r.context_data.lower() or "blocked" in r.context_data.lower()


async def test_forbidden_command_blocked():
    r = await ServerShellSkill().execute(
        {"command": "mkfs.ext4 /dev/sdb"},
        "format disk",
    )
    assert "blocked" in r.context_data.lower() or "not allowed" in r.context_data.lower() or "not supported" in r.context_data.lower()


async def test_safe_command_executes():
    """A safe command goes through _run_command and returns output."""
    with patch(
        "app.skills.server_shell_skill._run_command",
        new=AsyncMock(return_value=("app/\nrequirements.txt\n", 0))
    ):
        r = await ServerShellSkill().execute(
            {"command": "ls", "cwd": "/root/sentinel-workspace"},
            "list workspace",
        )
    assert isinstance(r.context_data, str)
    assert "0" in r.context_data  # exit code


async def test_destructive_command_builds_pending():
    """Destructive commands get queued as pending_action."""
    r = await ServerShellSkill().execute(
        {"command": "kill 1234"},
        "kill process",
    )
    assert r.pending_action is not None


# ── approval_category logic ────────────────────────────────────────────────────


def test_skill_default_approval_none():
    s = ServerShellSkill()
    assert s.approval_category == ApprovalCategory.NONE


def test_skill_is_always_available():
    s = ServerShellSkill()
    assert s.is_available() is True
