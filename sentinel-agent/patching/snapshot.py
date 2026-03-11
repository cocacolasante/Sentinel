"""
Snapshot — git stash operations for pre-patch state capture.
"""

from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)
_STASH_REF_RE = re.compile(r"stash@\{(\d+)\}")


class Snapshot:
    @staticmethod
    async def create(app_dir: str) -> str | None:
        """
        Stash all changes (including untracked) before applying a patch.
        Returns the stash ref string (e.g. 'stash@{0}') or None on failure.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", app_dir,
                "stash", "push", "--include-untracked", "-m", "sentinel-agent-pre-patch",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode()
            if "Saved working directory" in output or "stash@" in output:
                # git stash returns the ref in stderr sometimes
                for text in (output, stderr.decode()):
                    m = _STASH_REF_RE.search(text)
                    if m:
                        ref = f"stash@{{{m.group(1)}}}"
                        logger.info("Snapshot created: %s", ref)
                        return ref
                return "stash@{0}"  # fallback
            logger.info("Nothing to stash (clean working tree)")
            return None
        except Exception as exc:
            logger.error("Snapshot create failed: %s", exc)
            return None

    @staticmethod
    async def restore(app_dir: str, stash_ref: str | None) -> bool:
        """Restore a stash snapshot (rollback)."""
        if not stash_ref:
            return True
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", app_dir, "stash", "pop",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            logger.info("Snapshot restored: %s", stash_ref)
            return True
        except Exception as exc:
            logger.error("Snapshot restore failed: %s", exc)
            return False
