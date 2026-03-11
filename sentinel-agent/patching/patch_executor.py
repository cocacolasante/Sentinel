"""
PatchExecutor — receives PATCH_INSTRUCTION from Brain and applies the patch.
"""

from __future__ import annotations

import asyncio
import logging

from patching.git_ops import GitOps
from patching.rollback import RollbackHandler
from patching.snapshot import Snapshot

logger = logging.getLogger(__name__)


class PatchExecutor:
    def __init__(self, settings, process_monitor):
        self._settings = settings
        self._process_monitor = process_monitor
        from patching.restart_handler import RestartHandler
        self._restart_handler = RestartHandler(settings, process_monitor)

    async def handle_patch_instruction(self, payload: dict) -> None:
        """
        Handle an inbound PATCH_INSTRUCTION message from Brain.
        Full workflow: verify → snapshot → apply → test → restart → result
        """
        patch_id = payload.get("patch_id", "unknown")
        diff_text = payload.get("diff", "")
        approved = payload.get("approved", False)
        app_dir = self._settings.app_dir

        logger.info("Received patch instruction: %s", patch_id)

        # Production gate
        if self._settings.sentinel_env == "production" and not approved:
            logger.warning("Patch %s rejected — production requires approval", patch_id)
            return

        # 1. Snapshot
        stash_ref = await Snapshot.create(app_dir)

        # 2. Apply diff
        applied = await GitOps.apply_diff(app_dir, diff_text, patch_id)
        if not applied:
            await RollbackHandler.rollback(None, app_dir, stash_ref, patch_id)
            logger.error("Patch %s apply failed — rolled back", patch_id)
            return

        # 3. Run tests (optional)
        test_cmd = self._settings.app_test_cmd
        if test_cmd:
            test_passed = await self._run_tests(test_cmd)
            if not test_passed:
                await RollbackHandler.rollback(None, app_dir, stash_ref, patch_id)
                logger.error("Patch %s tests failed — rolled back", patch_id)
                return

        # 4. Restart
        restarted = await self._restart_handler.restart(None, patch_id)
        if not restarted:
            await RollbackHandler.rollback(None, app_dir, stash_ref, patch_id)
            logger.error("Patch %s restart failed — rolled back", patch_id)
            return

        logger.info("Patch %s applied successfully", patch_id)

    async def _run_tests(self, test_cmd: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_shell(
                test_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._settings.app_dir,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                return False
            return proc.returncode == 0
        except Exception as exc:
            logger.error("Test run failed: %s", exc)
            return False

    def set_relay(self, relay) -> None:
        self._relay = relay
        self._restart_handler._relay = relay
