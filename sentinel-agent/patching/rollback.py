"""
RollbackHandler — restore pre-patch snapshot and notify Brain.
"""

from __future__ import annotations

import logging

from patching.snapshot import Snapshot

logger = logging.getLogger(__name__)


class RollbackHandler:
    @staticmethod
    async def rollback(relay, app_dir: str, stash_ref: str | None, patch_id: str) -> None:
        """Restore snapshot and send PATCH_RESULT rolled_back."""
        logger.warning("Rolling back patch %s", patch_id)
        success = await Snapshot.restore(app_dir, stash_ref)
        await relay.send("PATCH_RESULT", {
            "patch_id": patch_id,
            "success": False,
            "action": "rolled_back",
            "logs": f"Rollback {'succeeded' if success else 'failed'}",
        })
