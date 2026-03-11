"""
Sentinel Agent — main entry point.

Connects to Sentinel Brain via WebSocket and runs all monitors concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sentinel-agent")


async def main() -> None:
    from config import settings
    from core.relay import AgentRelay
    from core.heartbeat import HeartbeatLoop
    from core.identity import get_server_fingerprint
    from monitors.process_monitor import ProcessMonitor
    from monitors.log_monitor import LogMonitor
    from monitors.http_monitor import HttpMonitor
    from monitors.git_monitor import GitMonitor
    from monitors.resource_monitor import ResourceMonitor
    from patching.patch_executor import PatchExecutor

    if not settings.agent_id or not settings.agent_token:
        logger.error("AGENT_ID and AGENT_TOKEN must be set in /etc/sentinel-agent/env")
        sys.exit(1)

    relay = AgentRelay(settings)
    process_monitor = ProcessMonitor(settings)
    heartbeat = HeartbeatLoop(relay, settings)
    log_monitor = LogMonitor(settings)
    http_monitor = HttpMonitor(settings)
    git_monitor = GitMonitor(settings)
    resource_monitor = ResourceMonitor(settings)
    patch_executor = PatchExecutor(settings, process_monitor)
    patch_executor.set_relay(relay)

    from core.chat_handler import ChatCommandHandler
    chat_handler = ChatCommandHandler(relay, settings, process_monitor)

    # Register inbound handlers
    relay.register_handler("REGISTER_ACK", _handle_register_ack)
    relay.register_handler("PATCH_INSTRUCTION", patch_executor.handle_patch_instruction)
    relay.register_handler("CHAT_COMMAND", chat_handler.handle)

    # On connect: send REGISTER with server fingerprint + file list
    async def _on_connect():
        fingerprint = get_server_fingerprint()
        await relay.send("REGISTER", {
            "agent_id": settings.agent_id,
            "app_name": settings.app_name,
            "hostname": fingerprint["hostname"],
            "ip_address": fingerprint["ip_address"],
            "os_name": fingerprint["os_name"],
            "agent_version": "1.0.0",
            "sentinel_env": settings.sentinel_env,
            "file_tree": _collect_file_tree(settings.app_dir),
        })

    relay.register_handler("_on_connect", _on_connect)

    logger.info("Starting Sentinel Agent | app=%s env=%s", settings.app_name, settings.sentinel_env)

    await asyncio.gather(
        relay.connect_and_run(),
        heartbeat.run(),
        process_monitor.watch(relay),
        log_monitor.run(relay),
        http_monitor.poll(relay),
        git_monitor.poll(relay),
        resource_monitor.watch(relay),
        return_exceptions=True,
    )


async def _handle_register_ack(payload: dict) -> None:
    logger.info("Registered with Brain | status=%s", payload.get("status"))


def _collect_file_tree(app_dir: str) -> dict:
    """Collect a list of Python files for codebase indexing."""
    import os
    files = []
    try:
        for root, dirs, filenames in os.walk(app_dir):
            # Skip hidden dirs and common non-source dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".git", "venv", ".venv")]
            for name in filenames:
                if name.endswith((".py", ".js", ".ts", ".go", ".rs")):
                    path = os.path.join(root, name)
                    try:
                        with open(path) as f:
                            content = f.read(4000)  # first 4KB
                        files.append({"path": path, "content": content})
                    except Exception:
                        pass
    except Exception:
        pass
    return {"files": files[:50]}  # cap at 50


if __name__ == "__main__":
    asyncio.run(main())
