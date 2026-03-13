"""
GitCommitSkill — apply a validated patch to the live repo, commit, push a branch,
open a PR, and write a sentinel_audit row.

Wraps app/integrations/repo.py — no duplicate git logic.

Intent: git_commit (internal; called from self-heal pipeline)
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import subprocess
from pathlib import Path

from app.config import get_settings
from app.db import postgres
from app.integrations.slack_notifier import post_alert_sync
from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)
settings = get_settings()


class GitCommitSkill(BaseSkill):
    name = "git_commit"
    description = (
        "Apply a validated unified diff to the live repo, commit on a feature branch, "
        "push, open a PR, and log the action to sentinel_audit. "
        "Used internally by the self-heal pipeline."
    )
    trigger_intents = ["git_commit"]

    def is_available(self) -> bool:
        return bool(settings.github_token if hasattr(settings, "github_token") else True)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        diff_text: str = params.get("diff", "")
        test_id: str = params.get("test_id", "unknown")
        issue_slug: str = params.get("issue_slug", test_id.replace("/", "-").replace("::", "-")[:40])
        repo_path: str = params.get("repo_path", "/root/sentinel-workspace")
        session_id: str = params.get("session_id", "")

        if not diff_text:
            return SkillResult(context_data="No diff provided — nothing to commit.", is_error=True)

        branch = f"sentinel/self-heal-{issue_slug}"

        try:
            from app.integrations.repo import RepoClient

            repo = RepoClient()

            # Parse diff to find affected files and apply patches
            try:
                from unidiff import PatchSet

                patch_set = PatchSet.from_string(diff_text)
            except Exception as exc:
                return SkillResult(
                    context_data=f"Could not parse diff: {exc}",
                    is_error=True,
                )

            # Create branch
            await asyncio.to_thread(repo._create_branch_sync, branch)

            root = Path(repo_path)
            patched_files: list[str] = []

            for patched_file in patch_set:
                target_path = patched_file.path
                # Strip leading a/ or b/ from unified diff paths
                if target_path.startswith(("a/", "b/")):
                    target_path = target_path[2:]

                full_path = root / target_path
                if not full_path.exists():
                    logger.warning("Patch target not found: %s", target_path)
                    continue

                # Apply single-file diff
                file_diff = str(patched_file)
                result = subprocess.run(
                    ["patch", "-p1", "--forward"],
                    input=diff_text.encode(),
                    cwd=repo_path,
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    patched_files.append(target_path)
                else:
                    logger.warning(
                        "patch failed for %s: %s",
                        target_path,
                        result.stderr.decode(errors="replace")[:200],
                    )
                break  # patch -p1 applies whole diff at once; one subprocess call is enough

            if not patched_files:
                # Try applying the whole diff at once if individual file loop didn't run
                result = subprocess.run(
                    ["patch", "-p1"],
                    input=diff_text.encode(),
                    cwd=repo_path,
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    stderr = result.stderr.decode(errors="replace")[:300]
                    return SkillResult(
                        context_data=f"patch command failed: {stderr}",
                        is_error=True,
                    )
                # Collect files from patch set
                patched_files = [pf.path.lstrip("ab/") for pf in patch_set]

            # Commit
            commit_msg = f"fix: self-heal patch for {test_id}"
            await asyncio.to_thread(repo._commit_sync, commit_msg)

            # Push and open PR
            pr_url = await asyncio.to_thread(repo._push_sync, branch)

        except Exception as exc:
            logger.error("GitCommitSkill error: %s", exc, exc_info=True)
            return SkillResult(context_data=f"Git operation failed: {exc}", is_error=True)

        # Write sentinel_audit row
        try:
            postgres.execute(
                """
                INSERT INTO sentinel_audit (session_id, action, target, outcome, detail, pr_url)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    "self_heal",
                    test_id,
                    "pr_opened",
                    json.dumps({"diff_len": len(diff_text), "files": patched_files}),
                    pr_url or "",
                ),
            )
        except Exception as exc:
            logger.warning("sentinel_audit insert failed: %s", exc)

        # Slack notification
        try:
            msg = f"🔧 Self-heal PR opened: {pr_url}" if pr_url else f"🔧 Self-heal committed to branch `{branch}`"
            post_alert_sync(msg)
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)

        result_msg = f"PR opened: {pr_url}" if pr_url else f"Committed to branch {branch}"
        return SkillResult(context_data=result_msg)

    async def post_merge_hook(self, pr_number: int, skill_name: str | None = None) -> None:
        """Hot-reload a skill module after its PR is merged.

        Only evicts from sys.modules and re-imports — no disk writes, no shell commands.
        """
        import sys

        outcome = "success"
        if skill_name:
            module_key = f"app.skills.{skill_name}"
            try:
                # Evict from module cache
                if module_key in sys.modules:
                    del sys.modules[module_key]
                importlib.import_module(module_key)
                logger.info("post_merge_hook: reloaded %s", module_key)
            except Exception as exc:
                logger.error("post_merge_hook: failed to reload %s: %s", module_key, exc)
                outcome = "failed_reload"

        # Post Slack notification
        try:
            skill_label = skill_name or "unknown"
            post_alert_sync(
                f"🔀 PR #{pr_number} merged — `{skill_label}` reloaded"
            )
        except Exception as exc:
            logger.warning("post_merge_hook Slack notification failed: %s", exc)

        # Write sentinel_audit row
        try:
            await asyncio.to_thread(
                postgres.execute,
                """
                INSERT INTO sentinel_audit (action, target, outcome, detail)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    "post_merge_hook",
                    f"PR #{pr_number}",
                    outcome,
                    json.dumps({"pr_number": pr_number, "skill_name": skill_name}),
                ),
            )
        except Exception as exc:
            logger.warning("post_merge_hook audit insert failed: %s", exc)
