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
      | git\s+merge\b                                        # merge (must go via PR, not direct)
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

# ── .env write guardrail ───────────────────────────────────────────────────────
# Any command that writes to a .env file requires BREAKING-level approval.
_ENV_WRITE_RE = re.compile(
    r"""
    (                                        # output-redirect writes
        (?:>>?|tee\b)                        #   >, >>, tee
        \s*[\"']?                            #   optional quote
        (?:.*[/\\])?                         #   optional path prefix
        \.env(?:\.[a-z]+)?[\"']?             #   .env or .env.local etc.
    )
    |                                        # OR in-place edit tools
    (?:
        sed\s+-i                             #   sed -i
      | awk\s+.*>                            #   awk ... >
    )
    \s*[\"']?(?:.*[/\\])?\.env(?:\.[a-z]+)?[\"']?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ── Protected branch guardrail ────────────────────────────────────────────────
# Sentinel must NEVER push directly to main or master — all changes must go
# through the PR workflow so the owner can review before anything hits production.
# Checking out main/master then pushing, or running git merge, all constitute
# bypassing the PR gate and are unconditionally blocked.
_PROTECTED_BRANCH_RE = re.compile(
    # git push [remote] main  /  git push origin main  /  git push HEAD:main
    r"git\s+push\b[^|&;]*\b(?:main|master)\b"
    # git checkout main / git checkout master (switching to protected branch)
    r"|git\s+checkout\s+(?:main|master)\b",
    re.IGNORECASE,
)

# ── Protected path guardrail ───────────────────────────────────────────────────
# /root/sentinel is the host-side repo directory. The AI must only operate on
# /root/sentinel-workspace (the container bind-mount). Any command or path that
# references /root/sentinel (the raw host path), /sentinel-project (old mount),
# or bare /sentinel is unconditionally blocked.
_PROTECTED_PATH_RE = re.compile(
    r"/root/sentinel(?!/\s*-\s*workspace)(?:/|\s|['\"]|$)"  # /root/sentinel (not -workspace)
    r"|/sentinel-project(?:/|\s|['\"]|$)"  # old mount point
    r"|(?<![a-zA-Z0-9_\-])/sentinel(?:/|\s|['\"]|$)",  # bare /sentinel
    re.IGNORECASE,
)

# Use the bind-mounted live host code when available, fall back to container /app
_CODE_ROOT = "/root/sentinel-workspace" if os.path.isdir("/root/sentinel-workspace") else "/app"
_DEFAULT_CWD = _CODE_ROOT
_MAX_OUTPUT = 8_000  # chars


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
        output = (stdout or b"").decode("utf-8", errors="replace")
        code = proc.returncode or 0
    except asyncio.TimeoutError:
        output = "[Command timed out after 120 seconds]"
        code = -1
    except FileNotFoundError:
        output = f"[Working directory not found: {cwd}]"
        code = -1

    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + f"\n... [output truncated at {_MAX_OUTPUT} chars]"

    return output, code


class ServerShellSkill(BaseSkill):
    name = "server_shell"
    description = (
        "Execute server operations: read files, run shell commands, list directories, search code, "
        "restart Docker services, manage git. Use when Anthony says 'run command', 'check the logs', "
        "'read file [path]', 'restart [service]', 'git status', 'list files in', 'search code for', "
        "'what's in [directory]', or 'show running containers'. "
        "Safety guardrails prevent destructive operations. "
        "NOT for: deploying applications (use deploy_skill), code changes with git workflow "
        "(use repo_write/repo_commit), or remote server commands (use agent_exec)."
    )
    trigger_intents = ["server_shell"]
    approval_category = ApprovalCategory.NONE  # set dynamically per command

    def is_available(self) -> bool:
        return True  # always available — no external credentials required

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = (params.get("action") or "").strip().lower()
        cwd = (params.get("cwd") or _DEFAULT_CWD).rstrip("/") or _DEFAULT_CWD
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
            cwd = _DEFAULT_CWD

        elif action == "docker_restart":
            service = (params.get("service") or params.get("container") or "ai-brain").strip()
            command = f"docker restart {shlex.quote(service)}"
            cwd = _DEFAULT_CWD

        elif action == "docker_compose":
            sub_cmd = (params.get("sub_command") or params.get("command") or "ps").strip()
            compose_file = _CODE_ROOT + "/docker-compose.yml"
            command = f"docker compose -f {shlex.quote(compose_file)} {sub_cmd}"
            cwd = _CODE_ROOT

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
        _check_targets = [command, cwd] + [str(v) for k, v in params.items() if k in ("path", "search_path") and v]
        if any(_touches_protected_path(t) for t in _check_targets):
            return SkillResult(
                context_data=(
                    "[Blocked — /root/sentinel, /sentinel-project, and /sentinel are "
                    "protected paths that must never be accessed, modified, or deleted. "
                    "Use /root/sentinel-workspace for all file and shell operations.]"
                ),
                skill_name=self.name,
            )

        # Protected branch block — never push to main/master or check it out for modification.
        # ALL changes must be committed to a feature branch and submitted as a PR.
        # GitHub branch protection AND this code-level block both enforce this rule.
        if _PROTECTED_BRANCH_RE.search(command):
            return SkillResult(
                context_data=(
                    "[Blocked — pushing directly to main/master or checking out a protected branch "
                    "is forbidden. Follow the mandatory PR workflow:\n"
                    "  1. Stay on your feature branch (feat/<name> or fix/<name>)\n"
                    "  2. git push origin <branch-name>  (NOT origin main)\n"
                    "  3. Open a PR via github_write or the GitHub API targeting base=main\n"
                    "  4. Report the PR URL to the owner — changes go live only after their approval.\n"
                    "NEVER run: git checkout main, git push origin main, or git merge to main.]"
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

        # .env write guard — requires BREAKING-level approval
        if _ENV_WRITE_RE.search(command):
            # Elevate this skill's approval category so the dispatcher always confirms
            self.approval_category = ApprovalCategory.BREAKING  # type: ignore[assignment]
            pending = {
                "intent": "server_shell",
                "action": "shell_exec",
                "params": params,
                "original": original_message,
            }
            context = (
                "⚠️ This command writes to a `.env` file and requires explicit approval:\n\n"
                f"```bash\n{command}\n```\n"
                "`.env` modifications can expose secrets or break the system. "
                "Reply **confirm** to execute or **cancel** to abort."
            )
            return SkillResult(
                context_data=context,
                pending_action=pending,
                skill_name=self.name,
            )

        # Destructive commands → confirmation flow
        if _is_destructive(command):
            pending = {
                "intent": "server_shell",
                "action": "shell_exec",
                "params": params,
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
        _is_milestone = action in ("docker_restart", "docker_compose") or bool(_MILESTONE_CMD_RE.search(command))
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
