"""
Agent Installer — shared helper for automatic agent bootstrapping.

Called by any skill or integration that provisions or connects to a new server:
  • IONOS provision_server()
  • IONOS deploy_website()
  • IONOS deploy_docker_app()

Installs two agents on every new server:
  1. MeshCentral agent  — RMM / remote desktop / power management
  2. Sentinel Mesh Agent — AI-driven monitoring, patching, and command relay

Both installations are best-effort: a failure is logged and recorded in the
caller's result dict but never raises an exception to the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets

logger = logging.getLogger(__name__)


async def _provision_agent_credentials(host: str, app_name: str, sentinel_env: str) -> dict:
    """Insert a mesh_agents row and return {agent_id, agent_token}."""
    from app.db import postgres

    raw_secret = secrets.token_hex(32)
    secret_hash = hashlib.sha256(raw_secret.encode()).hexdigest()

    row = await asyncio.to_thread(
        postgres.execute_one,
        """
        INSERT INTO mesh_agents (app_name, sentinel_env, ip_address, hmac_secret)
        VALUES (%s, %s, %s, %s)
        RETURNING agent_id
        """,
        (app_name, sentinel_env, host, secret_hash),
    )
    if not row:
        raise RuntimeError("DB insert for mesh_agents returned no row")
    return {"agent_id": str(row["agent_id"]), "agent_token": raw_secret}


async def install_agents_on_server(
    ionos_client,
    host: str,
    app_name: str,
    sentinel_env: str = "staging",
    username: str = "root",
) -> dict:
    """
    Install MeshCentral + Sentinel Mesh Agent on a remote server via SSH.

    Always returns a dict — never raises.  The caller should inspect
    result["meshcentral"]["status"] and result["sentinel_agent"]["status"].
    """
    from app.config import get_settings

    s = get_settings()
    result: dict = {
        "host": host,
        "meshcentral": {"status": "skipped"},
        "sentinel_agent": {"status": "skipped"},
    }

    # ── 1. MeshCentral agent ──────────────────────────────────────────────────
    try:
        from app.integrations.meshcentral import MeshCentralClient

        mc = MeshCentralClient()
        mesh_id = s.meshcentral_default_mesh_id
        if mc.is_configured() and mesh_id:
            install_cmd = mc.get_agent_install_command(mesh_id, "linux")
            mc_r = await ionos_client.ssh_exec(host, install_cmd, username=username, timeout=120)
            if mc_r.get("exit_code") == 0:
                result["meshcentral"] = {"status": "installed"}
                logger.info("MeshCentral agent installed | host=%s", host)
            else:
                err = (mc_r.get("stderr") or mc_r.get("stdout") or "")[:300]
                result["meshcentral"] = {"status": "failed", "error": err}
                logger.warning("MeshCentral install failed | host=%s | %s", host, err)
        else:
            result["meshcentral"]["reason"] = "not configured"
    except Exception as exc:
        logger.warning("MeshCentral install skipped | host=%s | %s", host, exc)
        result["meshcentral"] = {"status": "skipped", "reason": str(exc)}

    # ── 2. Sentinel Mesh Agent ────────────────────────────────────────────────
    try:
        creds = await _provision_agent_credentials(host, app_name, sentinel_env)
        agent_id = creds["agent_id"]
        agent_token = creds["agent_token"]

        repo_url = s.sentinel_agent_repo_url or "https://github.com/cocacolasante/Sentinel.git"
        brain_ws = f"wss://{s.domain}/ws/agent"

        # Clone the repo, detect whether sentinel-agent/ is a subdirectory
        # (full Sentinel monorepo) or the root (standalone repo), then run install.sh.
        install_cmd = (
            "export DEBIAN_FRONTEND=noninteractive && "
            "apt-get install -yq git python3 python3-venv 2>/dev/null || true && "
            "rm -rf /tmp/_sa_install && "
            f"git clone --depth 1 {repo_url} /tmp/_sa_install && "
            "SA_DIR=$( [ -d /tmp/_sa_install/sentinel-agent ] && "
            "echo /tmp/_sa_install/sentinel-agent || echo /tmp/_sa_install ) && "
            f"AGENT_ID={agent_id} "
            f"AGENT_TOKEN={agent_token} "
            f"BRAIN_URL={brain_ws} "
            f"APP_NAME={app_name} "
            f"SENTINEL_ENV={sentinel_env} "
            "bash $SA_DIR/install.sh"
        )

        sa_r = await ionos_client.ssh_exec(host, install_cmd, username=username, timeout=240)
        if sa_r.get("exit_code") == 0:
            result["sentinel_agent"] = {"status": "installed", "agent_id": agent_id}
            logger.info("Sentinel agent installed | host=%s | agent_id=%s", host, agent_id)
        else:
            err = (sa_r.get("stderr") or sa_r.get("stdout") or "")[:400]
            result["sentinel_agent"] = {"status": "failed", "agent_id": agent_id, "error": err}
            logger.warning("Sentinel agent install failed | host=%s | %s", host, err)
    except Exception as exc:
        logger.warning("Sentinel agent install skipped | host=%s | %s", host, exc)
        result["sentinel_agent"] = {"status": "skipped", "reason": str(exc)}

    return result
