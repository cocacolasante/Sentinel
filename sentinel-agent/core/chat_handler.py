"""
ChatCommandHandler — handles CHAT_COMMAND messages from Sentinel Brain.

Supported commands:
  read_logs       — tail app_log_path (default 100 lines, max 500)
  process_status  — psutil process info for app_process_name
  disk_usage      — disk stats for app_dir (or custom path)
  shell           — run arbitrary shell command (blocked on production without force=True)
  restart_app     — restart using app_restart_cmd (requires approved=True on production)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


class ChatCommandHandler:
    def __init__(self, relay, settings, process_monitor):
        self._relay = relay
        self._settings = settings
        self._process_monitor = process_monitor
        from patching.restart_handler import RestartHandler
        self._restart_handler = RestartHandler(settings, process_monitor)

        self._commands: dict[str, Callable] = {
            "read_logs":      self._cmd_read_logs,
            "process_status": self._cmd_process_status,
            "disk_usage":     self._cmd_disk_usage,
            "shell":          self._cmd_shell,
            "restart_app":    self._cmd_restart_app,
        }

    async def handle(self, payload: dict) -> None:
        correlation_id = payload.get("correlation_id", "")
        command = payload.get("command", "")
        args = payload.get("args", {})

        logger.info("CHAT_COMMAND received: %s (corr=%s)", command, correlation_id)
        t0 = time.time()

        handler = self._commands.get(command)
        if not handler:
            await self._send_response(correlation_id, command, False, None,
                                      f"Unknown command: {command!r}. "
                                      f"Valid: {', '.join(self._commands)}", t0)
            return

        try:
            result = await handler(args)
            await self._send_response(correlation_id, command, True, result, None, t0)
        except Exception as exc:
            logger.error("CHAT_COMMAND %s failed: %s", command, exc)
            await self._send_response(correlation_id, command, False, None, str(exc), t0)

    async def _send_response(
        self,
        correlation_id: str,
        command: str,
        success: bool,
        result,
        error,
        t0: float,
    ) -> None:
        elapsed_ms = int((time.time() - t0) * 1000)
        await self._relay.send("CHAT_RESPONSE", {
            "correlation_id": correlation_id,
            "command": command,
            "success": success,
            "result": result,
            "error": error,
            "elapsed_ms": elapsed_ms,
        })

    # ── Command implementations ────────────────────────────────────────────────

    async def _cmd_read_logs(self, args: dict) -> dict:
        import os
        log_path = self._settings.app_log_path
        if not log_path or not os.path.exists(log_path):
            return {
                "output": "",
                "lines_read": 0,
                "log_path": log_path or "app_log_path not configured",
            }

        lines_req = min(int(args.get("lines", 100)), 500)
        proc = await asyncio.create_subprocess_shell(
            f"tail -n {lines_req} {log_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            return {"output": "timeout reading logs", "lines_read": 0, "log_path": log_path}

        output = stdout.decode(errors="replace")
        return {
            "output": output,
            "lines_read": output.count("\n"),
            "log_path": log_path,
        }

    async def _cmd_process_status(self, args: dict) -> dict:
        import psutil
        process_name = self._settings.app_process_name
        if not process_name:
            return {
                "running": False, "pid": None, "cpu_pct": 0.0, "mem_mb": 0.0,
                "message": "app_process_name not configured",
            }

        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
            try:
                if process_name.lower() in proc.info["name"].lower():
                    mem_info = proc.info["memory_info"]
                    mem_mb = round(mem_info.rss / 1024 / 1024, 2) if mem_info else 0.0
                    return {
                        "running": True,
                        "pid": proc.info["pid"],
                        "cpu_pct": proc.cpu_percent(interval=0.1),
                        "mem_mb": mem_mb,
                        "status": proc.info["status"],
                        "process_name": process_name,
                    }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return {
            "running": False, "pid": None, "cpu_pct": 0.0, "mem_mb": 0.0,
            "process_name": process_name,
        }

    async def _cmd_disk_usage(self, args: dict) -> dict:
        import psutil
        path = args.get("path", self._settings.app_dir or "/")
        usage = psutil.disk_usage(path)
        return {
            "total_gb": round(usage.total / 1024 ** 3, 2),
            "used_gb":  round(usage.used  / 1024 ** 3, 2),
            "free_gb":  round(usage.free  / 1024 ** 3, 2),
            "pct":      usage.percent,
            "path":     path,
        }

    async def _cmd_shell(self, args: dict) -> dict:
        cmd = args.get("cmd", "")
        if not cmd:
            return {"stdout": "", "stderr": "cmd argument required", "exit_code": 1, "timed_out": False}

        if self._settings.sentinel_env == "production" and not args.get("force"):
            return {
                "stdout": "",
                "stderr": "Shell commands on production agents require args.force=true",
                "exit_code": 1,
                "timed_out": False,
            }

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            timed_out = True
            stdout, stderr = b"", b"Command timed out after 30s"

        return {
            "stdout":    stdout.decode(errors="replace")[:10000],
            "stderr":    stderr.decode(errors="replace")[:2000],
            "exit_code": proc.returncode if not timed_out else -1,
            "timed_out": timed_out,
        }

    async def _cmd_restart_app(self, args: dict) -> dict:
        if self._settings.sentinel_env == "production" and not args.get("approved"):
            return {"success": False, "message": "Production restart requires approved=true"}

        restarted = await self._restart_handler.restart(None, "chat_command")
        return {
            "success": restarted,
            "message": "App restarted successfully" if restarted else "Restart failed or process did not recover",
        }
