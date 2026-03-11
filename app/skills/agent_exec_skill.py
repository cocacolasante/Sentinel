"""
AgentExecSkill — execute commands on a remote Sentinel Mesh Agent.

Registered in the Brain Dispatcher so any session (Slack, REST, Grafana)
can invoke local agent operations through the existing CHAT_COMMAND Redis
relay. The Brain calls this skill automatically when the intent classifier
routes to 'agent_exec' — typically when the user is in an agent chat
session and requests something that must run ON the remote server.

Supported commands (delegated to sentinel-agent/core/chat_handler.py):
  shell, read_logs, process_status, disk_usage, restart_app,
  read_file, list_files, write_file, env_info
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid

import redis.asyncio as aioredis
from loguru import logger

from app.config import get_settings
from app.db import postgres
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()

POLL_TIMEOUT = 30        # seconds to wait for agent response
POLL_INTERVAL = 0.5      # seconds between polls


class AgentExecSkill(BaseSkill):
    """Execute arbitrary commands on a connected remote Sentinel Mesh Agent."""

    name = "agent_exec"
    description = (
        "Execute a command on a remote Sentinel Mesh Agent: run shell commands, "
        "read logs/files, check process status, disk usage, restart the app, "
        "list directory contents, or get env info. Use when the user asks to "
        "run something on a specific remote agent or server."
    )
    trigger_intents = [
        "agent_exec", "remote_exec", "agent_shell",
        "agent_logs", "agent_process", "agent_files",
    ]
    approval_category = ApprovalCategory.STANDARD

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        agent_id = params.get("agent_id", "")
        command = params.get("command", "shell")
        args = params.get("args", {})
        if isinstance(args, str):
            args = {"cmd": args}

        if not agent_id:
            return SkillResult(
                context_data=(
                    "agent_id is required for remote execution. "
                    "If you're in an agent session, ensure the [AGENT CONTEXT] block is present."
                )
            )

        try:
            result = await self._execute_on_agent(agent_id, command, args)
            return SkillResult(context_data=result)
        except Exception as exc:
            logger.error("AgentExecSkill error agent={} cmd={}: {}", agent_id, command, exc)
            return SkillResult(context_data=f"Remote execution failed: {exc}", is_error=True)

    async def _execute_on_agent(self, agent_id: str, command: str, args: dict) -> str:
        agent = await asyncio.to_thread(
            postgres.execute_one,
            "SELECT app_name, hmac_secret, sentinel_env, is_connected "
            "FROM mesh_agents WHERE agent_id = %s AND is_revoked = FALSE",
            (agent_id,),
        )
        if not agent:
            return f"Agent `{agent_id}` not found or has been revoked."
        if not agent["is_connected"]:
            return f"Agent `{agent['app_name']}` is offline — command cannot be dispatched."

        correlation_id = str(uuid.uuid4())
        ts = time.time()
        payload = {
            "correlation_id": correlation_id,
            "command": command,
            "args": args,
            "issued_by": "brain_skill",
            "issued_at": ts,
        }
        canonical = (
            f"{ts}:CHAT_COMMAND:"
            f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
        )
        sig = hmac.new(
            agent["hmac_secret"].encode(), canonical.encode(), "sha256"
        ).hexdigest()
        cmd_msg = json.dumps({"type": "CHAT_COMMAND", "payload": payload, "ts": ts, "sig": sig})

        redis = aioredis.from_url(
            f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/0",
            decode_responses=True,
        )
        raw = None
        try:
            await redis.rpush(f"sentinel:agent:cmd:{agent_id}", cmd_msg)
            await redis.expire(f"sentinel:agent:cmd:{agent_id}", 3600)
            await redis.set(
                f"sentinel:agent:chat_pending:{agent_id}:{correlation_id}",
                "pending",
                ex=POLL_TIMEOUT + 10,
            )

            iters = int(POLL_TIMEOUT / POLL_INTERVAL)
            for _ in range(iters):
                await asyncio.sleep(POLL_INTERVAL)
                resp_key = f"sentinel:agent:chat_response:{agent_id}:{correlation_id}"
                raw = await redis.get(resp_key)
                if raw:
                    await redis.delete(resp_key)
                    break
        finally:
            await redis.aclose()

        if not raw:
            return (
                f"Command `{command}` on `{agent['app_name']}` timed out after {POLL_TIMEOUT}s. "
                "The agent may be busy or the network latency is high."
            )

        data = json.loads(raw)
        return _format_exec_result(agent["app_name"], command, data)


def _format_exec_result(app_name: str, command: str, data: dict) -> str:
    ok = data.get("success", False)
    result = data.get("result") or {}
    error = data.get("error")
    elapsed = data.get("elapsed_ms", 0)
    icon = "✅" if ok else "❌"
    header = f"**{app_name}** — `{command}` {icon} ({elapsed}ms)"

    if not ok:
        return f"{header}\nError: {error}"

    if command == "read_logs":
        output = result.get("output", "")
        lines = result.get("lines_read", 0)
        path = result.get("log_path", "")
        tail = output[-4000:] if len(output) > 4000 else output
        return f"{header}\nPath: `{path}` | {lines} lines\n```\n{tail}\n```"

    if command == "process_status":
        running = result.get("running", False)
        pid = result.get("pid")
        cpu = result.get("cpu_pct", 0)
        mem = result.get("mem_mb", 0)
        status = result.get("status", "")
        dot = "🟢" if running else "🔴"
        return (
            f"{header}\n{dot} {'UP' if running else 'DOWN'} "
            f"| PID {pid} | CPU {cpu:.1f}% | Mem {mem:.1f}MB | {status}"
        )

    if command == "disk_usage":
        pct = result.get("pct", 0)
        used = result.get("used_gb", 0)
        total = result.get("total_gb", 0)
        path = result.get("path", "/")
        bar_len = 20
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        return f"{header}\n`{path}` [{bar}] {pct:.1f}% — {used:.1f}GB / {total:.1f}GB"

    if command == "shell":
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        code = result.get("exit_code", 0)
        timed_out = result.get("timed_out", False)
        parts = [f"{header} | exit={code}"]
        if timed_out:
            parts.append("⏱️ Command timed out after 30s")
        if stdout:
            tail = stdout[-4000:] if len(stdout) > 4000 else stdout
            parts.append(f"```\n{tail}\n```")
        if stderr:
            tail = stderr[-1000:] if len(stderr) > 1000 else stderr
            parts.append(f"STDERR:\n```\n{tail}\n```")
        return "\n".join(parts)

    if command == "restart_app":
        return f"{header}\n{result.get('message', '')}"

    if command == "read_file":
        content = result.get("content", "")
        path = result.get("path", "")
        size = result.get("size_bytes", 0)
        tail = content[-4000:] if len(content) > 4000 else content
        return f"{header}\n`{path}` ({size} bytes)\n```\n{tail}\n```"

    if command == "list_files":
        files = result.get("files", [])
        path = result.get("path", "")
        lines = "\n".join(f"  {f}" for f in files[:100])
        return f"{header}\n`{path}` ({len(files)} items)\n```\n{lines}\n```"

    if command == "env_info":
        info = result.get("info", {})
        lines = "\n".join(f"  {k}: {v}" for k, v in list(info.items())[:30])
        return f"{header}\n```\n{lines}\n```"

    # Generic fallback
    return f"{header}\n```json\n{json.dumps(result, indent=2)[:3000]}\n```"
