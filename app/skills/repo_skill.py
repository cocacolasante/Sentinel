"""
RepoSkill — lets the Brain read and modify its own codebase.

Read actions  (repo_read)   — list files, read file, diff, status
Write actions (repo_write)  — write/patch a file  → requires confirmation
Commit actions(repo_commit) — commit + push       → requires confirmation

Approval categories:
  repo_read   — NONE      (reading never needs approval)
  repo_write  — CRITICAL  (file edits need approval at level ≤ 2)
  repo_commit — CRITICAL  (commits/pushes need approval at level ≤ 2)
"""

from __future__ import annotations

import json

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
            )

        # Ensure the repo is cloned / up to date
        await client.ensure_repo()

        action = params.get("action", "status")
        path   = params.get("path", "")

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
            return SkillResult(context_data="[Repo not configured]", skill_name=self.name)

        action  = params.get("action", "write_file")
        path    = params.get("path", "")
        content = params.get("content", "")
        old     = params.get("old", "")
        new     = params.get("new", "")

        if not path:
            return SkillResult(
                context_data="[repo_write requires a path]", skill_name=self.name
            )

        pending = {
            "intent":   "repo_write",
            "action":   "write_file" if action != "patch_file" else "patch_file",
            "params":   params,
            "original": original_message,
        }

        if action == "patch_file":
            preview = (
                f"Patch `{path}`:\n"
                f"  Replace: {repr(old[:80])}{'…' if len(old)>80 else ''}\n"
                f"  With:    {repr(new[:80])}{'…' if len(new)>80 else ''}"
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
            return SkillResult(context_data="[Repo not configured]", skill_name=self.name)

        action  = params.get("action", "commit_push")
        message = params.get("message", "Brain: automated update")
        push    = action in ("push", "commit_push")

        # Show current diff so the user knows what will be committed
        diff = await client.diff()
        pending = {
            "intent":   "repo_commit",
            "action":   "commit_push" if push else "commit",
            "params":   params,
            "original": original_message,
        }

        context = (
            f"Show the user what is about to be committed and ask for confirmation:\n\n"
            f"Commit message: \"{message}\"\n"
            f"Push to GitHub: {'Yes' if push else 'No'}\n\n"
            f"Changes:\n{diff[:1500]}{'…' if len(diff)>1500 else ''}\n\n"
            "Reply **confirm** to commit (and push) or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
