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
import re
import subprocess
from pathlib import Path

from loguru import logger
from app.config import get_settings

settings = get_settings()

SSH_KEY = Path(settings.repo_ssh_key_path)

# ── Protected path guardrail ───────────────────────────────────────────────────
_PROTECTED_DIR = Path("/root/sentinel")

# ── Secret scanner ─────────────────────────────────────────────────────────────
_ENV_PATH_RE = re.compile(r"(^|[/\\])\.env(\.[a-z]+)?$", re.IGNORECASE)

_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub PAT
    re.compile(r"sk-ant-[A-Za-z0-9\-]{90,}"),  # Anthropic key
    re.compile(r"xoxb-[0-9]+-[A-Za-z0-9]+"),  # Slack bot token
    re.compile(r"xapp-[0-9]+-[A-Za-z0-9]+"),  # Slack app token
    re.compile(r"AKIA[A-Z0-9]{16}"),  # AWS key
    re.compile(r"sntryu_[A-Za-z0-9]{64}"),  # Sentry token
    re.compile(r"(?i)(password|secret|token)\s*=\s*[^\s]{8,}"),  # generic
]


def _scan_secrets(diff: str) -> list[str]:
    """Return a list of matched secret patterns found in *diff*, or empty list."""
    if not diff:
        return []
    matches: list[str] = []
    for pat in _SECRET_PATTERNS:
        if pat.search(diff):
            matches.append(pat.pattern[:40])
    return matches


def _dm_secret_leak(patterns: list[str]) -> None:
    """DM the owner and post an alert when a potential secret leak is detected."""
    try:
        from app.integrations.slack_notifier import post_dm_sync, post_alert_sync

        msg = (
            "🚨 *Secret leak prevented*\n"
            "A git push was aborted because the diff contained potential secrets:\n"
            + "\n".join(f"  • `{p}`" for p in patterns[:5])
        )
        post_dm_sync(msg)
        post_alert_sync(msg)
    except Exception as exc:
        logger.warning("Could not send secret-leak DM: %s", exc)


def _open_pr_sync(branch: str, title: str, body: str) -> tuple[str, int]:
    """Open a GitHub PR from *branch* → main. Returns (pr_url, pr_number)."""
    import httpx

    token = settings.github_token
    repo = settings.github_default_repo  # "owner/repo"
    if not token or not repo:
        logger.warning("GitHub token or repo not configured — skipping PR creation")
        return ("(PR not created — GitHub not configured)", 0)

    payload = {
        "title": title[:256],
        "body": body[:65_536],
        "head": branch,
        "base": "main",
        "draft": False,
    }
    with httpx.Client(timeout=15) as client:
        r = client.post(
            f"https://api.github.com/repos/{repo}/pulls",
            json=payload,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if r.status_code == 201:
        data = r.json()
        return (data["html_url"], data["number"])
    # PR already exists → find it
    if r.status_code == 422:
        with httpx.Client(timeout=15) as client:
            r2 = client.get(
                f"https://api.github.com/repos/{repo}/pulls",
                params={"head": f"{repo.split('/')[0]}:{branch}", "state": "open"},
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if r2.status_code == 200 and r2.json():
            existing = r2.json()[0]
            return (existing["html_url"], existing["number"])
    logger.warning("GitHub PR creation failed: {} {}", r.status_code, r.text[:200])
    return (f"(PR creation failed: {r.status_code})", 0)


def _notify_pr_slack(pr_url: str, branch: str, pr_number: int) -> None:
    """DM the owner and post to sentinel-alerts when a sentinel PR is opened."""
    try:
        from app.integrations.slack_notifier import post_dm_sync, post_alert_sync

        msg = (
            f"🔀 *Sentinel opened PR #{pr_number} — review required*\n"
            f"Branch: `{branch}`\n"
            f"Link: {pr_url}\n\n"
            "_Merge to deploy. No changes will reach production until you approve._"
        )
        post_dm_sync(msg)
        post_alert_sync(msg)
    except Exception as exc:
        logger.warning("Could not send PR notification: {}", exc)


def _assert_not_protected(path: Path) -> None:
    """Raise PermissionError if *path* is inside /root/sentinel or is a .env file."""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    if resolved == _PROTECTED_DIR or str(resolved).startswith(str(_PROTECTED_DIR) + "/"):
        raise PermissionError(
            f"Access denied: /root/sentinel is a protected path. "
            f"All file operations must target /root/sentinel-workspace."
        )
    # Block writes to .env and .env.* files — they contain secrets
    if _ENV_PATH_RE.search(str(resolved)):
        raise PermissionError(
            "Modifying .env files requires explicit user approval. Use the approval flow instead of writing directly."
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
        env["GIT_SSH_COMMAND"] = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
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
                    capture_output=True,
                    text=True,
                    env=_git_env(),
                )
                self._run(["git", "config", "user.email", "brain@csuitecode.com"])
                self._run(["git", "config", "user.name", "AI Brain"])
                return f"Cloned {remote_url} to {self._workspace}"
            out = self._run(["git", "pull", "--ff-only"])
            return out or "Already up to date"

        # Local mode: just verify the directory exists
        if not self._workspace.exists():
            raise RuntimeError(
                f"Repo directory not found: {self._workspace}. Set GITHUB_BRAIN_REPO_URL or REPO_LOCAL_PATH in .env."
            )
        # Ensure git identity is set (needed for commits)
        try:
            self._run(["git", "config", "user.email", "brain@csuitecode.com"], check=False)
            self._run(["git", "config", "user.name", "AI Brain"], check=False)
        except Exception:
            pass
        return f"Using local repo at {self._workspace}"

    def _status_sync(self) -> str:
        return self._run(["git", "status", "--short"])

    def _diff_sync(self) -> str:
        staged = self._run(["git", "diff", "--staged"], check=False)
        unstaged = self._run(["git", "diff"], check=False)
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
            files = sorted(str(p.relative_to(self._workspace)) for p in target.rglob("*") if p.is_file())
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
        numbered = "\n".join(f"{i + 1:4} | {l}" for i, l in enumerate(lines))
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
        # Auto-push and open a PR so every commit goes through human review
        pr_out = self._push_sync(pr_title=message)
        return f"{out}\n{pr_out}"

    def _push_sync(self, pr_title: str = "", pr_body: str = "") -> str:
        """
        Push Sentinel's changes as a feature branch and open a PR for human review.

        Rules:
        - Never push directly to main/master.
        - If we're already on a sentinel/* branch, push it.
        - If we're on main/master (or detached HEAD), create a new sentinel/patch-* branch first.
        - After pushing, open a GitHub PR and DM the owner for review.
        """
        # ── Secret scan before push ────────────────────────────────────────────
        try:
            diff = self._run(["git", "diff", "HEAD~1..HEAD"], check=False)
        except Exception:
            diff = self._run(["git", "diff", "--staged"], check=False)
        leaked = _scan_secrets(diff)
        if leaked:
            _dm_secret_leak(leaked)
            raise PermissionError(
                "Push aborted — potential secrets detected in diff: "
                + ", ".join(leaked[:3])
                + (f" (+{len(leaked) - 3} more)" if len(leaked) > 3 else "")
            )
        # ── .env in staged files ───────────────────────────────────────────────
        staged_files = self._run(["git", "diff", "--name-only", "--cached"], check=False)
        if any(_ENV_PATH_RE.search(ln.strip()) for ln in staged_files.splitlines()):
            _dm_secret_leak([".env file staged for commit"])
            raise PermissionError("Push aborted — a .env file is staged. Remove it with: git reset HEAD .env")

        # ── Ensure we're on a feature branch, never main ──────────────────────
        import time as _time

        current = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
        if current in ("main", "master", "HEAD"):
            branch = f"sentinel/patch-{int(_time.time())}"
            self._run(["git", "checkout", "-b", branch])
            logger.info("Created sentinel branch: {}", branch)
        else:
            branch = current

        # ── Push branch to origin ─────────────────────────────────────────────
        self._run(["git", "push", "origin", branch, "--set-upstream"])
        logger.info("Pushed branch {} to origin", branch)

        # ── Open PR via GitHub API ─────────────────────────────────────────────
        pr_url, pr_number = _open_pr_sync(
            branch=branch,
            title=pr_title or f"sentinel: {branch}",
            body=pr_body or (
                "Automated changes proposed by Sentinel.\n\n"
                "**Please review carefully before merging.** "
                "Merging will trigger CI and deploy to production."
            ),
        )

        # ── Notify owner ──────────────────────────────────────────────────────
        _notify_pr_slack(pr_url, branch, pr_number)

        return f"Opened PR #{pr_number}: {pr_url}"

    def _create_branch_sync(self, branch: str) -> str:
        """Create and checkout a new branch, resetting to origin/main first."""
        self._run(["git", "fetch", "origin", "main"], check=False)
        self._run(["git", "checkout", "-B", branch, "origin/main"])
        return branch

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

    async def push(self, pr_title: str = "", pr_body: str = "") -> str:
        return await asyncio.to_thread(self._push_sync, pr_title, pr_body)

    async def create_branch(self, branch: str) -> str:
        return await asyncio.to_thread(self._create_branch_sync, branch)
