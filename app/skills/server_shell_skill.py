"""
ServerShellSkill — full shell access to the host server.

Allows the AI to navigate the filesystem, create directories and projects,
run builds, inspect processes, and manage files.

Safety model:
  - Safe commands (read / navigate / build / create) execute immediately.
  - Destructive commands (rm -rf, kill, etc.) require explicit user confirmation.

Working directory:
  Pass `cwd` in params to set the working directory for the command.
  The AI maintains cwd across turns via the conversation context.
  Default cwd is /root (home directory of the running container).

Output:
  Stdout + stderr are returned, truncated to 8 000 chars if very long.
  Exit code is always reported.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

# ── Milestone-worthy safe command detection ────────────────────────────────────
# Safe commands that change state (not just reads) are worth logging as milestones.
_MILESTONE_CMD_RE = re.compile(
    r"\b(git\s+(commit|push|pull|clone|checkout|merge)\b"
    r"|docker\s+(restart|build|start|compose)\b"
    r"|npm\s+(install|run|build|start)\b"
    r"|pip\s+install\b"
    r"|apt(?:-get)?\s+install\b"
    r"|systemctl\s+start\b"
    r"|make\b)",
    re.IGNORECASE,
)

# ── Destructive pattern detection ─────────────────────────────────────────────
# These patterns require explicit user confirmation before execution.
_DESTRUCTIVE_RE = re.compile(
    r"""
    \b(
        rm\s+(-[^-\s]*[rf][^-\s]*\s+|--recursive|--force)  # rm -rf / rm -r / rm -f
      | rmdir\b                                              # remove directory
      | kill\b | pkill\b | killall\b                        # kill processes
      | dd\b                                                 # disk dump (dangerous)
      | truncate\b                                           # truncate files
      | mkfs\b                                               # format filesystem
      | fdisk\b | parted\b                                   # partition tools
      | shred\b                                              # secure delete
      | git\s+push\s+.*--force                               # force push
      | git\s+reset\s+--hard                                 # hard reset
      | git\s+clean\s+-[^-]*f                                # force clean
      | docker\s+(rm|rmi|stop|kill)\b                        # docker destructive
      | systemctl\s+(stop|disable|mask)\b                    # stop services
      | chmod\s+777\b                                        # world-writable
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ── Safe command allowlist (execute immediately) ───────────────────────────────
# Anything NOT matching the destructive pattern AND matching safe patterns runs
# directly. We also block a few truly dangerous categories outright.
_FORBIDDEN_RE = re.compile(
    r"\b(mkfs|fdisk|parted|shred|dd\s+if=.*of=/dev)\b",
    re.IGNORECASE,
)

# ── Protected path guardrail ───────────────────────────────────────────────────
# /root/sentinel is the host-side repo directory. The AI must only operate on
# /root/sentinel-workspace (the container bind-mount). Any command or path that
# references /root/sentinel (the raw host path), /sentinel-project (old mount),
# or bare /sentinel is unconditionally blocked.
_PROTECTED_PATH_RE = re.compile(
    r"/root/sentinel(?!/\s*-\s*workspace)(?:/|\s|['\"]|$)"   # /root/sentinel (not -workspace)
    r"|/sentinel-project(?:/|\s|['\"]|$)"                     # old mount point
    r"|(?<![a-zA-Z0-9_\-])/sentinel(?:/|\s|['\"]|$)",         # bare /sentinel
    re.IGNORECASE,
)

# Use the bind-mounted live host code when available, fall back to container /app
_CODE_ROOT   = "/root/sentinel-workspace" if os.path.isdir("/root/sentinel-workspace") else "/app"
_DEFAULT_CWD = _CODE_ROOT
_MAX_OUTPUT  = 8_000   # chars


def _is_destructive(command: str) -> bool:
    return bool(_DESTRUCTIVE_RE.search(command))


def _is_forbidden(command: str) -> bool:
    return bool(_FORBIDDEN_RE.search(command))


def _touches_protected_path(text: str) -> bool:
    """Return True if *text* contains a reference to the protected /root/sentinel tree."""
    return bool(_PROTECTED_PATH_RE.search(text))


async def _run_command(command: str, cwd: str) -> tuple[str, int]:
    """Run *command* in *cwd* via bash, return (combined_output, exit_code)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            executable="/bin/bash",
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        output    = (stdout or b"").decode("utf-8", errors="replace")
        code      = proc.returncode or 0
    except asyncio.TimeoutError:
        output = "[Command timed out after 120 seconds]"
        code   = -1
    except FileNotFoundError:
        output = f"[Working directory not found: {cwd}]"
        code   = -1

    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + f"\n... [output truncated at {_MAX_OUTPUT} chars]"

    return output, code


class ServerShellSkill(BaseSkill):
    name        = "server_shell"
    description = (
        "Execute shell commands on the server — navigate filesystem, read/write files, "
        "search code with grep, create directories, run builds (npm, pip, docker), "
        "inspect processes and logs, scaffold projects, push to GitHub, restart Docker services. "
        "Actions: read_file, search_code, list_files, inspect_env, docker_restart, docker_compose. "
        "Pass command= for raw shell. Destructive commands (rm -rf, kill, etc.) require confirmation. "
        "git push/commit/pull, docker restart, and docker compose all execute immediately."
    )
    trigger_intents  = ["server_shell"]
    approval_category = ApprovalCategory.NONE   # set dynamically per command

    def is_available(self) -> bool:
        return True   # always available — no external credentials required

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action     = (params.get("action") or "").strip().lower()
        cwd        = (params.get("cwd") or _DEFAULT_CWD).rstrip("/") or _DEFAULT_CWD
        background = str(params.get("background", "false")).lower() in ("true", "1", "yes")
        session_id = (params.get("session_id") or "").strip()

        # ── Convenience actions (no raw command needed) ────────────────────────
        if action == "read_file":
            path = (params.get("path") or "").strip()
            if not path:
                return SkillResult(
                    context_data="[read_file requires a 'path' param]",
                    skill_name=self.name,
                )
            # Accept absolute paths or paths relative to the code root
            if not path.startswith("/"):
                path = f"{_CODE_ROOT}/{path}"
            command = f"cat -n {shlex.quote(path)}"

        elif action == "search_code":
            pattern = (params.get("pattern") or params.get("query") or "").strip()
            search_path = (params.get("path") or _CODE_ROOT).strip()
            if not search_path.startswith("/"):
                search_path = f"{_CODE_ROOT}/{search_path}"
            if not pattern:
                return SkillResult(
                    context_data="[search_code requires a 'pattern' param]",
                    skill_name=self.name,
                )
            command = (
                f"grep -rn --include='*.py' --include='*.yaml' --include='*.yml' "
                f"--include='*.json' --include='*.md' "
                f"-i {shlex.quote(pattern)} {shlex.quote(search_path)} 2>/dev/null | head -80"
            )

        elif action == "list_files":
            path = (params.get("path") or _CODE_ROOT).strip()
            if not path.startswith("/"):
                path = f"{_CODE_ROOT}/{path}"
            command = f"find {shlex.quote(path)} -type f | sort | head -100 2>/dev/null"

        elif action == "inspect_env":
            # Show all environment variables so the AI knows what integrations are configured.
            command = "printenv | sort"
            cwd     = _DEFAULT_CWD

        elif action == "docker_restart":
            service = (
                params.get("service") or params.get("container") or "ai-brain"
            ).strip()
            command = f"docker restart {shlex.quote(service)}"
            cwd     = _DEFAULT_CWD

        elif action == "docker_compose":
            sub_cmd = (params.get("sub_command") or params.get("command") or "ps").strip()
            compose_file = _CODE_ROOT + "/docker-compose.yml"
            command = f"docker compose -f {shlex.quote(compose_file)} {sub_cmd}"
            cwd     = _CODE_ROOT

        else:
            command = (params.get("command") or "").strip()

        if not command:
            return SkillResult(
                context_data=(
                    "[server_shell requires either a 'command' param or an 'action' of "
                    "read_file / search_code / list_files. Ask the user what they want to run.]"
                ),
                skill_name=self.name,
            )

        # Protected path block — /root/sentinel must never be accessed or modified
        _check_targets = [command, cwd] + [
            str(v) for k, v in params.items() if k in ("path", "search_path") and v
        ]
        if any(_touches_protected_path(t) for t in _check_targets):
            return SkillResult(
                context_data=(
                    "[Blocked — /root/sentinel, /sentinel-project, and /sentinel are "
                    "protected paths that must never be accessed, modified, or deleted. "
                    "Use /root/sentinel-workspace for all file and shell operations.]"
                ),
                skill_name=self.name,
            )

        # Hard block
        if _is_forbidden(command):
            return SkillResult(
                context_data=(
                    f"[Command blocked — '{command}' is a low-level disk operation "
                    "that could irreversibly damage the server. Not supported.]"
                ),
                skill_name=self.name,
            )

        # Destructive commands → confirmation flow
        if _is_destructive(command):
            pending = {
                "intent":  "server_shell",
                "action":  "shell_exec",
                "params":  params,
                "original": original_message,
            }
            context = (
                f"This command is potentially destructive and requires confirmation:\n\n"
                f"```bash\n{command}\n```\n"
                f"Working directory: `{cwd}`\n\n"
                "Show the user exactly what will run and ask them to reply **confirm** "
                "to execute or **cancel** to abort."
            )
            return SkillResult(
                context_data=context,
                pending_action=pending,
                skill_name=self.name,
            )

        # Background mode — queue via Celery and return immediately
        if background and session_id:
            slack_ctx = None
            try:
                from app.memory.redis_client import RedisMemory
                slack_ctx = RedisMemory().get_slack_context(session_id)
            except Exception:
                pass

            if slack_ctx:
                from app.worker.tasks import run_shell_and_report_back
                run_shell_and_report_back.delay(
                    commands=[command],
                    channel=slack_ctx["channel"],
                    thread_ts=slack_ctx["thread_ts"],
                    cwd=cwd,
                    label=action or "shell command",
                )
                return SkillResult(
                    context_data=(
                        f"Background task queued.\n"
                        f"Command: `{command}`\n"
                        f"Working directory: `{cwd}`\n"
                        "I'll post the result back to this Slack thread when it completes."
                    ),
                    skill_name=self.name,
                )

        # Safe command — execute immediately
        output, code = await _run_command(command, cwd)
        status = "✅ exit 0" if code == 0 else f"⚠️ exit {code}"
        context = (
            f"Command executed on server.\n"
            f"```bash\n$ {command}\n```\n"
            f"Working directory: `{cwd}`\n"
            f"Status: {status}\n\n"
            f"Output:\n```\n{output or '(no output)'}\n```"
        )

        # Log milestone for write-type safe commands (not pure reads like cat/ls/grep)
        _is_milestone = (
            action in ("docker_restart", "docker_compose")
            or bool(_MILESTONE_CMD_RE.search(command))
        )
        if code == 0 and _is_milestone:
            async def _notify() -> None:
                try:
                    from app.integrations.milestone_logger import log_milestone
                    await log_milestone(
                        action=action or "shell_command",
                        intent="server_shell",
                        params={"command": command, "cwd": cwd},
                        session_id="system",
                        agent="brain",
                    )
                except Exception:
                    pass
            asyncio.create_task(_notify())

        return SkillResult(context_data=context, skill_name=self.name)
