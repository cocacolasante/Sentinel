"""
ChatCommandHandler — handles CHAT_COMMAND messages from Sentinel Brain.

All commands run locally on the agent's server and return a CHAT_RESPONSE.
Brain's AgentExecSkill dispatches these and surfaces results through the
full Dispatcher pipeline (all 50+ Brain skills are accessible to agents).

Supported commands:
  shell           — run arbitrary shell command (30s timeout; blocked prod w/o force=True)
  read_logs       — tail app_log_path (default 100 lines, max 500)
  process_status  — psutil process info for app_process_name
  disk_usage      — disk stats for a path (defaults to app_dir)
  restart_app     — execute app_restart_cmd (requires approved=True in production)
  read_file       — read contents of a file path
  write_file      — write/append content to a file (requires approved=True in production)
  list_files      — list files in a directory
  env_info        — non-sensitive environment / system info
"""

from __future__ import annotations

import asyncio
import logging
import os
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
            "shell":          self._cmd_shell,
            "read_logs":      self._cmd_read_logs,
            "process_status": self._cmd_process_status,
            "disk_usage":     self._cmd_disk_usage,
            "restart_app":    self._cmd_restart_app,
            "read_file":      self._cmd_read_file,
            "write_file":     self._cmd_write_file,
            "list_files":     self._cmd_list_files,
            "env_info":       self._cmd_env_info,
        }

    async def handle(self, payload: dict) -> None:
        correlation_id = payload.get("correlation_id", "")
        command = payload.get("command", "")
        args = payload.get("args", {})

        logger.info("CHAT_COMMAND: %s (corr=%s)", command, correlation_id)
        t0 = time.time()

        handler = self._commands.get(command)
        if not handler:
            await self._respond(correlation_id, command, False, None,
                                f"Unknown command: {command!r}. Valid: {', '.join(self._commands)}",
                                t0)
            return

        try:
            result = await handler(args)
            await self._respond(correlation_id, command, True, result, None, t0)
        except Exception as exc:
            logger.error("CHAT_COMMAND %s failed: %s", command, exc)
            await self._respond(correlation_id, command, False, None, str(exc), t0)

    async def _respond(self, correlation_id, command, success, result, error, t0):
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

    async def _cmd_shell(self, args: dict) -> dict:
        cmd = args.get("cmd", "")
        if not cmd:
            return {"stdout": "", "stderr": "cmd argument required", "exit_code": 1, "timed_out": False}
        if self._settings.sentinel_env == "production" and not args.get("force"):
            return {
                "stdout": "",
                "stderr": "Shell commands on production agents require args.force=true",
                "exit_code": 1, "timed_out": False,
            }
        timeout = int(args.get("timeout", 30))
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            timed_out = True
            stdout, stderr = b"", f"Command timed out after {timeout}s".encode()

        return {
            "stdout":    stdout.decode(errors="replace")[:10000],
            "stderr":    stderr.decode(errors="replace")[:2000],
            "exit_code": proc.returncode if not timed_out else -1,
            "timed_out": timed_out,
        }

    async def _cmd_read_logs(self, args: dict) -> dict:
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
        return {"output": output, "lines_read": output.count("\n"), "log_path": log_path}

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
        return {"running": False, "pid": None, "cpu_pct": 0.0, "mem_mb": 0.0, "process_name": process_name}

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

    async def _cmd_restart_app(self, args: dict) -> dict:
        if self._settings.sentinel_env == "production" and not args.get("approved"):
            return {"success": False, "message": "Production restart requires approved=true"}
        restarted = await self._restart_handler.restart(None, "chat_command")
        return {
            "success": restarted,
            "message": "App restarted successfully" if restarted else "Restart failed or process did not recover",
        }

    async def _cmd_read_file(self, args: dict) -> dict:
        path = args.get("path", "")
        if not path:
            return {"content": "", "size_bytes": 0, "path": "", "error": "path argument required"}
        # Restrict to app_dir for safety unless force=true
        app_dir = self._settings.app_dir or "/"
        if not os.path.abspath(path).startswith(os.path.abspath(app_dir)) and not args.get("force"):
            return {
                "content": "", "size_bytes": 0, "path": path,
                "error": f"Path is outside app_dir ({app_dir}). Use force=true to override.",
            }
        try:
            size = os.path.getsize(path)
            max_bytes = int(args.get("max_bytes", 50000))
            with open(path, errors="replace") as f:
                content = f.read(max_bytes)
            return {"content": content, "size_bytes": size, "path": path, "truncated": size > max_bytes}
        except Exception as e:
            return {"content": "", "size_bytes": 0, "path": path, "error": str(e)}

    async def _cmd_write_file(self, args: dict) -> dict:
        if self._settings.sentinel_env == "production" and not args.get("approved"):
            return {"success": False, "message": "File writes in production require approved=true"}
        path = args.get("path", "")
        content = args.get("content", "")
        mode = "a" if args.get("append") else "w"
        if not path:
            return {"success": False, "message": "path argument required"}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, mode) as f:
                f.write(content)
            return {"success": True, "path": path, "bytes_written": len(content.encode())}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def _cmd_list_files(self, args: dict) -> dict:
        path = args.get("path", self._settings.app_dir or ".")
        pattern = args.get("pattern", "")
        recursive = args.get("recursive", False)
        try:
            if recursive:
                import fnmatch
                files = []
                for root, dirs, fnames in os.walk(path):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".git", "venv")]
                    for fn in fnames:
                        fp = os.path.relpath(os.path.join(root, fn), path)
                        if not pattern or fnmatch.fnmatch(fn, pattern):
                            files.append(fp)
                        if len(files) >= 200:
                            break
            else:
                entries = os.listdir(path)
                files = sorted(entries)[:200]
            return {"files": files, "path": path, "count": len(files)}
        except Exception as e:
            return {"files": [], "path": path, "count": 0, "error": str(e)}

    async def _cmd_env_info(self, args: dict) -> dict:
        import platform, psutil
        # Only expose non-sensitive env vars (app config, not tokens/passwords)
        safe_vars = {
            k: v for k, v in os.environ.items()
            if not any(x in k.upper() for x in ("TOKEN", "SECRET", "PASSWORD", "KEY", "PASS", "AUTH"))
        }
        try:
            mem = psutil.virtual_memory()
            cpu_count = psutil.cpu_count()
            return {
                "info": {
                    "hostname": platform.node(),
                    "os": platform.platform(),
                    "python": platform.python_version(),
                    "cpu_cores": cpu_count,
                    "total_mem_gb": round(mem.total / 1024**3, 2),
                    "app_name": self._settings.app_name,
                    "app_dir": self._settings.app_dir,
                    "sentinel_env": self._settings.sentinel_env,
                    **{k: v for k, v in list(safe_vars.items())[:20]},
                }
            }
        except Exception as e:
            return {"info": {"error": str(e)}}
