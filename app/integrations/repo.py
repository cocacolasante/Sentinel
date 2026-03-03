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

# Workspace inside the container (mounted volume)
WORKSPACE = Path(settings.repo_workspace)
SSH_KEY   = Path(settings.repo_ssh_key_path)

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
        cwd=str(cwd or WORKSPACE),
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return (result.stdout + result.stderr).strip()


class RepoClient:
    def is_configured(self) -> bool:
        return bool(settings.github_brain_repo_url)

    # ── Sync internals ────────────────────────────────────────────────────────

    def _ensure_repo_sync(self) -> str:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        git_dir = WORKSPACE / ".git"
        if not git_dir.exists():
            url = settings.github_brain_repo_url
            logger.info("Cloning Brain repo | url={}", url)
            _run(["git", "clone", url, str(WORKSPACE)], cwd=WORKSPACE.parent)
            _run(["git", "config", "user.email", "brain@csuitecode.com"])
            _run(["git", "config", "user.name",  "AI Brain"])
            return f"Cloned {url} to {WORKSPACE}"
        else:
            out = _run(["git", "pull", "--ff-only"])
            return out or "Already up to date"

    def _status_sync(self) -> str:
        return _run(["git", "status", "--short"])

    def _diff_sync(self) -> str:
        staged   = _run(["git", "diff", "--staged"], check=False)
        unstaged = _run(["git", "diff"],             check=False)
        parts = []
        if staged:
            parts.append(f"=== Staged ===\n{staged}")
        if unstaged:
            parts.append(f"=== Unstaged ===\n{unstaged}")
        return "\n\n".join(parts) or "(no changes)"

    def _list_files_sync(self, path: str = "") -> str:
        target = (WORKSPACE / path) if path else WORKSPACE
        if not target.exists():
            raise FileNotFoundError(f"Path not found in repo: {path}")
        try:
            out = _run(["git", "ls-files", str(target)])
            lines = [l.replace(str(WORKSPACE) + "/", "") for l in out.splitlines()]
            return "\n".join(lines) if lines else "(empty)"
        except Exception:
            # Fall back to find if git ls-files fails
            files = sorted(str(p.relative_to(WORKSPACE)) for p in target.rglob("*") if p.is_file())
            return "\n".join(files) or "(empty)"

    def _read_file_sync(self, path: str) -> str:
        full = WORKSPACE / path
        if not full.exists():
            raise FileNotFoundError(f"File not found: {path}")
        content = full.read_text(errors="replace")
        # Return with line numbers for easier LLM reference
        lines = content.splitlines()
        numbered = "\n".join(f"{i+1:4} | {l}" for i, l in enumerate(lines))
        return f"=== {path} ({len(lines)} lines) ===\n{numbered}"

    def _write_file_sync(self, path: str, content: str) -> str:
        full = WORKSPACE / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return f"Written: {path} ({len(content.splitlines())} lines)"

    def _patch_file_sync(self, path: str, old: str, new: str) -> str:
        full = WORKSPACE / path
        if not full.exists():
            raise FileNotFoundError(f"File not found: {path}")
        original = full.read_text()
        if old not in original:
            raise ValueError(f"Target text not found in {path} — patch aborted")
        patched = original.replace(old, new, 1)
        full.write_text(patched)
        return f"Patched: {path}"

    def _commit_sync(self, message: str) -> str:
        _run(["git", "add", "-A"])
        status = _run(["git", "status", "--short"])
        if not status:
            return "Nothing to commit — working tree clean"
        out = _run(["git", "commit", "-m", message])
        return out

    def _push_sync(self) -> str:
        return _run(["git", "push", "origin", "HEAD"])

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

    async def commit(self, message: str) -> str:
        return await asyncio.to_thread(self._commit_sync, message)

    async def push(self) -> str:
        return await asyncio.to_thread(self._push_sync)
