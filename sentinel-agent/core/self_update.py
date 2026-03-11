"""
SelfUpdateHandler — handles SELF_UPDATE commands from Sentinel Brain.

When the sentinel-agent source code changes on the main repo, Brain
broadcasts a SELF_UPDATE to all connected agents. Each agent:
  1. Checks if it's already on the target SHA — skips if so
  2. git fetch + git reset --hard to the target SHA
  3. pip install if requirements.txt changed
  4. Sends SELF_UPDATE_RESULT back to Brain
  5. Restarts the agent daemon

Source directory is auto-detected from this file's location, or
overridden via AGENT_SOURCE_DIR in /etc/sentinel-agent/env.

Restart behaviour (in order of priority):
  1. AGENT_RESTART_CMD env var (e.g. "systemctl restart sentinel-agent")
  2. os.execv — in-process Python restart (works everywhere)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

logger = logging.getLogger(__name__)


def _source_dir(settings) -> str:
    """Return the root directory of the sentinel-agent source tree."""
    configured = getattr(settings, "agent_source_dir", "")
    if configured and os.path.isdir(configured):
        return configured
    # Auto-detect: self_update.py lives at {root}/core/self_update.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _run(cmd: str, cwd: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run a shell command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"Command timed out after {timeout}s: {cmd}"
    return proc.returncode, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()


class SelfUpdateHandler:
    def __init__(self, relay, settings):
        self._relay = relay
        self._settings = settings

    async def handle(self, payload: dict) -> None:
        correlation_id = payload.get("correlation_id", "")
        target_sha = payload.get("target_sha", "")
        branch = payload.get("branch", "main")
        force = payload.get("force", False)

        logger.info("SELF_UPDATE received: target_sha=%s branch=%s", target_sha or "latest", branch)
        t0 = time.time()

        result = await self._do_update(target_sha, branch, force)
        elapsed_ms = int((time.time() - t0) * 1000)

        await self._relay.send("SELF_UPDATE_RESULT", {
            "correlation_id": correlation_id,
            "success": result["success"],
            "old_sha": result.get("old_sha", ""),
            "new_sha": result.get("new_sha", ""),
            "message": result.get("message", ""),
            "elapsed_ms": elapsed_ms,
        })

        if result["success"] and result.get("old_sha") != result.get("new_sha"):
            logger.info("Self-update applied — restarting agent daemon...")
            await asyncio.sleep(1)   # let SELF_UPDATE_RESULT transmit
            await self._restart()
        elif result["success"]:
            logger.info("Already on target SHA — no restart needed")

    async def _do_update(self, target_sha: str, branch: str, force: bool) -> dict:
        src = _source_dir(self._settings)

        # 1. Get current SHA
        rc, old_sha, err = await _run("git rev-parse HEAD", src)
        if rc != 0:
            return {"success": False, "message": f"git rev-parse failed: {err}"}

        # 2. Skip if already on target
        if target_sha and not force and old_sha.startswith(target_sha[:12]):
            return {"success": True, "old_sha": old_sha, "new_sha": old_sha,
                    "message": f"Already on {old_sha[:12]} — skipping"}

        # 3. Fetch from origin
        rc, out, err = await _run(f"git fetch origin {branch}", src)
        if rc != 0:
            return {"success": False, "old_sha": old_sha,
                    "message": f"git fetch failed: {err}"}

        # 4. Reset to target SHA or origin/branch
        reset_ref = target_sha if target_sha else f"origin/{branch}"
        rc, out, err = await _run(f"git reset --hard {reset_ref}", src)
        if rc != 0:
            return {"success": False, "old_sha": old_sha,
                    "message": f"git reset --hard failed: {err}"}

        # 5. Get new SHA
        rc, new_sha, _ = await _run("git rev-parse HEAD", src)

        # 6. Install any new/updated dependencies
        req_file = os.path.join(src, "requirements.txt")
        pip_out = ""
        if os.path.exists(req_file):
            rc2, pip_out, pip_err = await _run(
                f"{sys.executable} -m pip install -r {req_file} -q",
                src,
                timeout=180,
            )
            if rc2 != 0:
                logger.warning("pip install had errors (continuing): %s", pip_err)
                pip_out = f"pip warnings: {pip_err[:200]}"

        msg = f"Updated {old_sha[:8]} → {new_sha[:8]}"
        if pip_out:
            msg += f" | {pip_out[:100]}"

        logger.info("Self-update: %s", msg)
        return {"success": True, "old_sha": old_sha, "new_sha": new_sha, "message": msg}

    async def _restart(self) -> None:
        """Restart the agent daemon process."""
        restart_cmd = getattr(self._settings, "agent_restart_cmd", "")
        if restart_cmd:
            logger.info("Restarting via: %s", restart_cmd)
            try:
                rc, _, err = await _run(restart_cmd, "/", timeout=15)
                if rc == 0:
                    # systemd (or similar) takes over — this process will be killed
                    await asyncio.sleep(5)
            except Exception as exc:
                logger.warning("Restart command failed (%s), falling back to exec restart", exc)

        # Fallback: in-process exec restart
        logger.info("Restarting via os.execv...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
