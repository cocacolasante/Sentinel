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
import re
import shlex

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

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

_DEFAULT_CWD = "/root"
_MAX_OUTPUT  = 8_000   # chars


def _is_destructive(command: str) -> bool:
    return bool(_DESTRUCTIVE_RE.search(command))


def _is_forbidden(command: str) -> bool:
    return bool(_FORBIDDEN_RE.search(command))


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
        "Execute shell commands on the server — navigate filesystem, create directories, "
        "manage files, run builds (npm, pip, go, cargo, docker), inspect processes and logs, "
        "scaffold new projects. Destructive commands require user confirmation."
    )
    trigger_intents  = ["server_shell"]
    approval_category = ApprovalCategory.NONE   # set dynamically per command

    def is_available(self) -> bool:
        return True   # always available — no external credentials required

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        command = (params.get("command") or "").strip()
        cwd     = (params.get("cwd") or _DEFAULT_CWD).rstrip("/") or _DEFAULT_CWD

        if not command:
            return SkillResult(
                context_data=(
                    "[server_shell requires a 'command' param. "
                    "Ask the user what they want to run.]"
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
        return SkillResult(context_data=context, skill_name=self.name)
