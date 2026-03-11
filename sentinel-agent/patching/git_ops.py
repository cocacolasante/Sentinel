"""
GitOps — apply unified diff patches and manage branches.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import os

logger = logging.getLogger(__name__)


class GitOps:
    @staticmethod
    async def apply_diff(app_dir: str, diff_text: str, patch_id: str) -> bool:
        """
        Apply a unified diff patch to the working tree.
        Returns True on success, False on failure.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
            f.write(diff_text)
            patch_file = f.name

        try:
            # Dry-run check
            check = await asyncio.create_subprocess_exec(
                "git", "-C", app_dir, "apply", "--check", patch_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(check.communicate(), timeout=15)
            if check.returncode != 0:
                logger.error("Patch check failed: %s", stderr.decode())
                return False

            # Apply
            apply = await asyncio.create_subprocess_exec(
                "git", "-C", app_dir, "apply", patch_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(apply.communicate(), timeout=15)
            if apply.returncode != 0:
                logger.error("Patch apply failed: %s", stderr.decode())
                return False

            # Commit
            await asyncio.create_subprocess_exec(
                "git", "-C", app_dir, "add", "-A",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            commit = await asyncio.create_subprocess_exec(
                "git", "-C", app_dir, "commit", "-m",
                f"sentinel-agent patch {patch_id}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(commit.communicate(), timeout=15)
            logger.info("Patch applied and committed: %s", patch_id)
            return True
        finally:
            os.unlink(patch_file)

    @staticmethod
    async def create_branch(app_dir: str, name: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", app_dir, "checkout", "-b", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0

    @staticmethod
    async def push(app_dir: str, branch: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", app_dir, "push", "origin", branch,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0
