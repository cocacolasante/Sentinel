"""
RepoSkill — lets the Brain read and modify its own codebase.

Read actions  (repo_read)    — list files, read file, diff, status
Write actions (repo_write)   — write/patch a file  → requires confirmation
Commit actions(repo_commit)  — commit + push       → requires confirmation
Code change   (code_change)  — full workflow: branch → patch → commit → push → PR + auto-merge

Approval categories:
  repo_read   — NONE      (reading never needs approval)
  repo_write  — CRITICAL  (file edits need approval at level ≤ 2)
  repo_commit — CRITICAL  (commits/pushes need approval at level ≤ 2)
  code_change — CRITICAL  (full workflow, auto-executes in BRAIN_AUTONOMY mode)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class RepoReadSkill(BaseSkill):
    name = "repo_read"
    description = "Browse, read, diff, or check status of the Brain's own codebase"
    trigger_intents = ["repo_read"]
    approval_category = ApprovalCategory.NONE

    def is_available(self) -> bool:
        from app.integrations.repo import RepoClient

        return RepoClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.repo import RepoClient

        client = RepoClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[Repo not configured — GITHUB_BRAIN_REPO_URL missing in .env]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        # Ensure the repo is cloned / up to date
        await client.ensure_repo()

        action = params.get("action", "status")
        path = params.get("path", "")

        if action == "list_files":
            data = await client.list_files(path)
        elif action == "read_file":
            if not path:
                return SkillResult(context_data="[read_file requires a path]", skill_name=self.name)
            data = await client.read_file(path)
        elif action == "diff":
            data = await client.diff()
        else:  # status (default)
            data = await client.status()

        return SkillResult(context_data=data, skill_name=self.name)


class RepoWriteSkill(BaseSkill):
    name = "repo_write"
    description = "Create or edit a file in the Brain's own codebase"
    trigger_intents = ["repo_write"]
    requires_confirmation = True
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.repo import RepoClient

        return RepoClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.repo import RepoClient

        if not RepoClient().is_configured():
            return SkillResult(
                context_data="[Repo not configured]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        action = params.get("action", "write_file")
        path = params.get("path", "")
        content = params.get("content", "")
        old = params.get("old", "")
        new = params.get("new", "")

        if not path:
            return SkillResult(context_data="[repo_write requires a path]", skill_name=self.name)

        pending = {
            "intent": "repo_write",
            "action": "write_file" if action != "patch_file" else "patch_file",
            "params": params,
            "original": original_message,
        }

        if action == "patch_file":
            preview = (
                f"Patch `{path}`:\n"
                f"  Replace: {repr(old[:80])}{'…' if len(old) > 80 else ''}\n"
                f"  With:    {repr(new[:80])}{'…' if len(new) > 80 else ''}"
            )
        else:
            lines = content.splitlines()
            preview = (
                f"Write `{path}` ({len(lines)} lines):\n"
                + "\n".join(f"  {l}" for l in lines[:8])
                + ("\n  …" if len(lines) > 8 else "")
            )

        context = (
            f"Show the user this proposed file change and ask them to confirm or cancel:\n\n"
            f"{preview}\n\n"
            "Reply **confirm** to apply the change or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )


class RepoCommitSkill(BaseSkill):
    name = "repo_commit"
    description = "Commit and/or push changes in the Brain's repository to GitHub"
    trigger_intents = ["repo_commit"]
    requires_confirmation = True
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.repo import RepoClient

        return RepoClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.repo import RepoClient

        client = RepoClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[Repo not configured]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        action = params.get("action", "commit_push")
        message = params.get("message", "Brain: automated update")
        push = action in ("push", "commit_push")

        # Show current diff so the user knows what will be committed
        diff = await client.diff()
        pending = {
            "intent": "repo_commit",
            "action": "commit_push" if push else "commit",
            "params": params,
            "original": original_message,
        }

        context = (
            f"Show the user what is about to be committed and ask for confirmation:\n\n"
            f'Commit message: "{message}"\n'
            f"Push to GitHub: {'Yes' if push else 'No'}\n\n"
            f"Changes:\n{diff[:1500]}{'…' if len(diff) > 1500 else ''}\n\n"
            "Reply **confirm** to commit (and push) or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )


class CodeChangeSkill(BaseSkill):
    """
    Full code-change workflow in a single skill call:
      1. git checkout -b <branch>
      2. patch_file (old → new)
      3. git add -A && git commit -m <message>
      4. git push origin HEAD
      5. gh pr create --base main
      6. gh pr merge --auto --squash
    Returns the PR URL so the caller can report it to the user.

    In BRAIN_AUTONOMY mode this executes immediately (CRITICAL approval is bypassed).
    Without autonomy, it goes through the normal confirmation flow.
    """

    name = "code_change"
    description = "Full self-modification workflow: branch → patch file → commit → push → open PR + auto-merge"
    trigger_intents = ["code_change"]
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.repo import RepoClient

        return RepoClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.repo import RepoClient
        from app.config import get_settings

        settings = get_settings()

        client = RepoClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[Repo not configured]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        branch = params.get("branch", "")
        path = params.get("path", "")
        old = params.get("old", "")
        new = params.get("new", "")
        message = params.get("commit_message", params.get("message", "chore: AI update"))
        pr_title = params.get("pr_title", message)
        pr_body = params.get("pr_body", params.get("description", original_message[:300]))

        if not branch or not path or old is None or new is None:
            return SkillResult(
                context_data=("[code_change requires: branch, path, old, new, commit_message, pr_title]"),
                skill_name=self.name,
            )

        preview = (
            f"**Code change proposal:**\n"
            f"- Branch: `{branch}`\n"
            f"- File: `{path}`\n"
            f"- Commit: `{message}`\n"
            f"- PR title: `{pr_title}`\n"
            f"- Replace: {repr(old[:120])}{'…' if len(old) > 120 else ''}\n"
            f"- With:    {repr(new[:120])}{'…' if len(new) > 120 else ''}"
        )

        pending = {
            "intent": "code_change",
            "action": "code_change",
            "params": params,
            "original": original_message,
        }

        if not settings.brain_autonomy:
            context = (
                f"{preview}\n\nReply **confirm** to create the branch, apply the patch, commit, push, and open a PR."
            )
            return SkillResult(context_data=context, pending_action=pending, skill_name=self.name)

        # Autonomy mode: execute the full workflow now
        result = await asyncio.to_thread(self._run_workflow, client, branch, path, old, new, message, pr_title, pr_body)
        return SkillResult(context_data=result, skill_name=self.name)

    def _run_workflow(
        self,
        client,
        branch,
        path,
        old,
        new,
        message,
        pr_title,
        pr_body,
    ) -> str:
        from app.integrations.repo import _git_env

        ws = client.workspace
        log = []

        def _sh(cmd: str) -> str:
            r = subprocess.run(
                cmd,
                shell=True,
                cwd=str(ws),
                capture_output=True,
                text=True,
                env=_git_env(),
            )
            out = (r.stdout + r.stderr).strip()
            log.append(f"$ {cmd}\n{out}" if out else f"$ {cmd}")
            if r.returncode != 0:
                raise RuntimeError(f"Command failed (exit {r.returncode}): {cmd}\n{out}")
            return out

        try:
            _sh("git checkout main")
            _sh("git pull --ff-only origin main")
            _sh(f"git checkout -b {branch}")

            full_path = ws / path
            if not full_path.exists():
                raise FileNotFoundError(f"File not found: {path}")
            content = full_path.read_text()
            if old not in content:
                raise ValueError(f"Target text not found in {path} — patch aborted")
            full_path.write_text(content.replace(old, new, 1))
            log.append(f"Patched: {path}")

            _sh("git add -A")
            _sh(f'git commit -m "{message}"')
            _sh(f"git push origin {branch}")

            pr_out = _sh(f'gh pr create --title "{pr_title}" --body "{pr_body}" --base main')
            pr_url = next((l.strip() for l in pr_out.splitlines() if "github.com" in l), pr_out.strip())
            _sh("gh pr merge --auto --squash")

            return (
                f"[Live data from code_change]\n"
                f"PR opened and auto-merge enabled.\n"
                f"URL: {pr_url}\n"
                f"The change will deploy automatically once CI passes (~3 min).\n\n"
                f"Steps completed:\n" + "\n".join(f"  {l.splitlines()[0]}" for l in log)
            )

        except Exception as exc:
            subprocess.run(
                "git checkout main",
                shell=True,
                cwd=str(ws),
                capture_output=True,
                env=_git_env(),
            )
            return f"[code_change failed]\nError: {exc}\n\nSteps before failure:\n" + "\n".join(
                f"  {l.splitlines()[0]}" for l in log
            )
