"""
Repo Integration — git operations on the Brain's own codebase.

The Brain clones its GitHub repo to REPO_WORKSPACE (/workspace/repo) inside
the container. From there it can read, edit, commit, and push changes.

Operations:
  ensure_repo()              — clone if not present, pull if already cloned
  status()                   — git status
  diff()                     — git diff (staged + unstaged)
  list_files(path)           — recursive file listing under path
  read_file(path)            — read a file from the workspace
  write_file(path, content)  — write/overwrite a file
  patch_file(path, old, new) — targeted in-place replacement
  commit(message)            — git add -A && git commit
  push()                     — git push origin HEAD
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from loguru import logger
from app.config import get_settings

settings = get_settings()

SSH_KEY = Path(settings.repo_ssh_key_path)

# ── Protected path guardrail ───────────────────────────────────────────────────
_PROTECTED_DIR = Path("/root/sentinel")


def _assert_not_protected(path: Path) -> None:
    """Raise PermissionError if *path* is inside /root/sentinel."""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    if resolved == _PROTECTED_DIR or str(resolved).startswith(str(_PROTECTED_DIR) + "/"):
        raise PermissionError(
            f"Access denied: /root/sentinel is a protected path. "
            f"All file operations must target /root/sentinel-workspace."
        )


def _resolve_workspace() -> Path:
    """
    Pick the working directory for all repo operations.

    Priority:
      1. If REPO_LOCAL_PATH exists and has a .git dir, use it directly —
         this is the bind-mounted live code directory inside the container.
      2. If GITHUB_BRAIN_REPO_URL is set, use REPO_WORKSPACE
         (a dedicated clone that gets pulled/committed independently).
      3. Fall back to REPO_WORKSPACE anyway (will be created on first use).
    """
    local = Path(settings.repo_local_path)
    if (local / ".git").exists():
        return local
    if settings.github_brain_repo_url:
        return Path(settings.repo_workspace)
    return Path(settings.repo_workspace)


# Env for all git subprocess calls — ensures the correct SSH key is used
def _git_env() -> dict:
    env = os.environ.copy()
    if SSH_KEY.exists():
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        )
    return env


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """Run a shell command synchronously and return stdout."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd or _resolve_workspace()),
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return (result.stdout + result.stderr).strip()


class RepoClient:
    """
    Git operations on the Brain's own codebase.

    Works in two modes:
      Remote mode  — GITHUB_BRAIN_REPO_URL is set.
                     Clone/pull into REPO_WORKSPACE, commit, push to GitHub.
      Local mode   — No remote URL, but REPO_LOCAL_PATH/.git exists.
                     Read/write/commit directly in the named-volume workspace.
                     Push still works if the remote is configured in that git repo.
    """

    def __init__(self) -> None:
        self._workspace = _resolve_workspace()

    def is_configured(self) -> bool:
        """Available whenever we can find a git repo to operate on."""
        if settings.github_brain_repo_url:
            return True
        local = Path(settings.repo_local_path)
        return (local / ".git").exists() or (self._workspace / ".git").exists()

    @property
    def workspace(self) -> Path:
        return self._workspace

    def _run(self, cmd: list[str], check: bool = True) -> str:
        result = subprocess.run(
            cmd,
            cwd=str(self._workspace),
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return (result.stdout + result.stderr).strip()

    # ── Sync internals ────────────────────────────────────────────────────────

    def _ensure_repo_sync(self) -> str:
        remote_url = settings.github_brain_repo_url

        if remote_url:
            # Remote mode: clone once, then pull
            self._workspace.mkdir(parents=True, exist_ok=True)
            if not (self._workspace / ".git").exists():
                logger.info("Cloning Brain repo | url={}", remote_url)
                subprocess.run(
                    ["git", "clone", remote_url, str(self._workspace)],
                    cwd=str(self._workspace.parent),
                    capture_output=True, text=True, env=_git_env(),
                )
                self._run(["git", "config", "user.email", "brain@csuitecode.com"])
                self._run(["git", "config", "user.name",  "AI Brain"])
                return f"Cloned {remote_url} to {self._workspace}"
            out = self._run(["git", "pull", "--ff-only"])
            return out or "Already up to date"

        # Local mode: just verify the directory exists
        if not self._workspace.exists():
            raise RuntimeError(
                f"Repo directory not found: {self._workspace}. "
                "Set GITHUB_BRAIN_REPO_URL or REPO_LOCAL_PATH in .env."
            )
        # Ensure git identity is set (needed for commits)
        try:
            self._run(["git", "config", "user.email", "brain@csuitecode.com"], check=False)
            self._run(["git", "config", "user.name",  "AI Brain"],             check=False)
        except Exception:
            pass
        return f"Using local repo at {self._workspace}"

    def _status_sync(self) -> str:
        return self._run(["git", "status", "--short"])

    def _diff_sync(self) -> str:
        staged   = self._run(["git", "diff", "--staged"], check=False)
        unstaged = self._run(["git", "diff"],             check=False)
        parts = []
        if staged:
            parts.append(f"=== Staged ===\n{staged}")
        if unstaged:
            parts.append(f"=== Unstaged ===\n{unstaged}")
        return "\n\n".join(parts) or "(no changes)"

    def _list_files_sync(self, path: str = "") -> str:
        if path and Path(path).is_absolute():
            target = Path(path)
        else:
            target = (self._workspace / path) if path else self._workspace
        _assert_not_protected(target)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        try:
            out = self._run(["git", "ls-files", str(target)])
            lines = [l.replace(str(self._workspace) + "/", "") for l in out.splitlines()]
            return "\n".join(lines) if lines else "(empty)"
        except Exception:
            files = sorted(
                str(p.relative_to(self._workspace))
                for p in target.rglob("*") if p.is_file()
            )
            return "\n".join(files) or "(empty)"

    def _read_file_sync(self, path: str) -> str:
        # Absolute paths are read directly from the filesystem (logs, configs, etc.)
        # Relative paths are resolved against the workspace (git repo root).
        if Path(path).is_absolute():
            full = Path(path)
        else:
            full = self._workspace / path
        _assert_not_protected(full)
        if not full.exists():
            raise FileNotFoundError(f"File not found: {path}")
        content = full.read_text(errors="replace")
        lines = content.splitlines()
        numbered = "\n".join(f"{i+1:4} | {l}" for i, l in enumerate(lines))
        return f"=== {path} ({len(lines)} lines) ===\n{numbered}"

    def _write_file_sync(self, path: str, content: str) -> str:
        full = self._workspace / path
        _assert_not_protected(full)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return f"Written: {path} ({len(content.splitlines())} lines)"

    def _patch_file_sync(self, path: str, old: str, new: str) -> str:
        full = self._workspace / path
        _assert_not_protected(full)
        if not full.exists():
            raise FileNotFoundError(f"File not found: {path}")
        original = full.read_text()
        if old not in original:
            raise ValueError(f"Target text not found in {path} — patch aborted")
        patched = original.replace(old, new, 1)
        full.write_text(patched)
        return f"Patched: {path}"

    def _commit_sync(self, message: str, files: list[str] | None = None) -> str:
        if files:
            # Stage only the specific files that were patched — never git add -A
            for f in files:
                self._run(["git", "add", "--", f])
        else:
            self._run(["git", "add", "-A"])
        status = self._run(["git", "status", "--short"])
        if not status:
            return "Nothing to commit — working tree clean"
        out = self._run(["git", "commit", "-m", message])
        return out

    def _push_sync(self) -> str:
        return self._run(["git", "push", "origin", "HEAD"])

    # ── Public async API ──────────────────────────────────────────────────────

    async def ensure_repo(self) -> str:
        return await asyncio.to_thread(self._ensure_repo_sync)

    async def status(self) -> str:
        return await asyncio.to_thread(self._status_sync)

    async def diff(self) -> str:
        return await asyncio.to_thread(self._diff_sync)

    async def list_files(self, path: str = "") -> str:
        return await asyncio.to_thread(self._list_files_sync, path)

    async def read_file(self, path: str) -> str:
        return await asyncio.to_thread(self._read_file_sync, path)

    async def write_file(self, path: str, content: str) -> str:
        return await asyncio.to_thread(self._write_file_sync, path, content)

    async def patch_file(self, path: str, old: str, new: str) -> str:
        return await asyncio.to_thread(self._patch_file_sync, path, old, new)

    async def commit(self, message: str, files: list[str] | None = None) -> str:
        return await asyncio.to_thread(self._commit_sync, message, files)

    async def push(self) -> str:
        return await asyncio.to_thread(self._push_sync)
