"""
CommandWithFallbackSkill — execute a sequence of shell commands with automatic
error recovery.

For each command in the chain:
  ✅ exit 0  → record success, continue to next command
  ❌ non-zero → parse the error, create a task on the task board, and attempt
                an LLM-assisted auto-fix before stopping the chain

Params:
  commands   list[str]  — ordered list of shell commands to run
  command    str        — single command (alias for commands=[command])
  cwd        str        — working directory (default: /root/sentinel-workspace)
  context    str        — optional human description of what this chain does
                          (used to make the auto-fix prompt more accurate)
  auto_fix   bool       — attempt an LLM-generated fix command on failure
                          (default: true)

Returns a SkillResult whose context_data contains a structured step-by-step
report so the LLM can relay exactly what succeeded and what failed.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult
from app.skills.server_shell_skill import (
    _DEFAULT_CWD,
    _is_destructive,
    _is_forbidden,
    _run_command,
    _touches_protected_path,
)

_MAX_ERROR_CONTEXT = 1_200  # chars of error output forwarded to the fix prompt
_MAX_STEP_OUTPUT = 600  # chars shown per step in the summary


# ── Error signature parser ─────────────────────────────────────────────────────

_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"command not found", re.I), "missing_binary"),
    (re.compile(r"no such file or directory", re.I), "missing_path"),
    (re.compile(r"permission denied", re.I), "permission_denied"),
    (re.compile(r"authentication failed|could not read.*password", re.I), "auth_failure"),
    (re.compile(r"connection refused|could not connect", re.I), "connection_refused"),
    (re.compile(r"name or service not known|network unreachable", re.I), "dns_or_network"),
    (re.compile(r"syntax error|parse error|unexpected token", re.I), "syntax_error"),
    (re.compile(r"exit status \d+|returned non-zero exit status", re.I), "subprocess_error"),
    (re.compile(r"npm err|yarn error", re.I), "npm_error"),
    (re.compile(r"pip.*error|could not install", re.I), "pip_error"),
    (re.compile(r"docker.*error|no such container", re.I), "docker_error"),
    (re.compile(r"git.*error|fatal:", re.I), "git_error"),
]


def _classify_error(output: str) -> str:
    for pattern, label in _ERROR_PATTERNS:
        if pattern.search(output):
            return label
    return "unknown_error"


def _format_step(index: int, command: str, output: str, code: int, fix_info: str = "") -> str:
    status = "✅ Success" if code == 0 else f"❌ Failed (exit {code})"
    out_snippet = (output[:_MAX_STEP_OUTPUT] + "…") if len(output) > _MAX_STEP_OUTPUT else output
    lines = [
        f"**Step {index + 1}** — `{command}`",
        f"Status: {status}",
    ]
    if out_snippet.strip():
        lines.append(f"Output:\n```\n{out_snippet.strip()}\n```")
    if fix_info:
        lines.append(fix_info)
    return "\n".join(lines)


# ── Auto-fix helper ────────────────────────────────────────────────────────────


async def _attempt_autofix(
    failed_cmd: str,
    error_output: str,
    error_type: str,
    cwd: str,
    chain_context: str,
) -> tuple[str, str]:
    """
    Ask the LLM for a single fix command, run it if safe, return
    (fix_summary_text, fix_command_or_empty).
    """
    from app.brain.llm_router import LLMRouter

    llm = LLMRouter()

    truncated = error_output[:_MAX_ERROR_CONTEXT]
    fix_prompt = (
        f"A shell command failed. Suggest ONE corrective bash command that would fix it.\n\n"
        f"Failed command: {failed_cmd}\n"
        f"Working directory: {cwd}\n"
        f"Error type: {error_type}\n"
        f"Error output:\n{truncated}\n"
        + (f"Chain context: {chain_context}\n" if chain_context else "")
        + "\nRespond with ONLY the fix command on a single line. No explanation. "
        "If no safe fix exists, respond with: NO_FIX"
    )

    try:
        raw = await asyncio.to_thread(llm.route, fix_prompt, [], None)
        fix_cmd = raw.strip().splitlines()[0].strip()
    except Exception as exc:
        return f"Auto-fix LLM call failed: {exc}", ""

    if fix_cmd in ("NO_FIX", "") or not fix_cmd:
        return "No automatic fix available for this error.", ""

    # Safety gates — don't auto-run destructive or forbidden commands
    if _is_forbidden(fix_cmd) or _is_destructive(fix_cmd) or _touches_protected_path(fix_cmd):
        return (
            f"Suggested fix `{fix_cmd}` was skipped — it is destructive or targets a protected path.",
            fix_cmd,
        )

    fix_output, fix_code = await _run_command(fix_cmd, cwd)
    if fix_code == 0:
        summary = (
            f"🔧 **Auto-fix applied** — `{fix_cmd}`\n```\n{fix_output[:_MAX_STEP_OUTPUT].strip() or '(no output)'}\n```"
        )
    else:
        snippet = fix_output[:_MAX_STEP_OUTPUT].strip()
        summary = (
            f"🔧 **Auto-fix attempted** — `{fix_cmd}` → still failed (exit {fix_code})\n"
            f"```\n{snippet or '(no output)'}\n```"
        )
    return summary, fix_cmd


# ── Task creation helper ───────────────────────────────────────────────────────


def _create_failure_task(
    failed_cmd: str,
    error_output: str,
    error_type: str,
    step_index: int,
    chain_context: str,
) -> str:
    """Insert a task into the DB for the failed step. Returns a short status string."""
    try:
        from app.db import postgres

        title = f"Auto-fix needed: {error_type} in step {step_index + 1}"
        description = (
            f"Command: {failed_cmd}\n"
            f"Error type: {error_type}\n"
            + (f"Context: {chain_context}\n" if chain_context else "")
            + f"Error output (truncated):\n{error_output[:500]}"
        )
        row = postgres.execute_one(
            """
            INSERT INTO tasks
                (title, description, status, priority, priority_num, approval_level, source, tags)
            VALUES (%s, %s, 'pending', 'high', 4, 1, 'command_with_fallback', 'auto-fix,shell-error')
            RETURNING id
            """,
            (title, description),
        )
        task_id = row["id"] if row else "?"
        return f"📋 Task #{task_id} created — {title}"
    except Exception as exc:
        return f"Task creation failed: {exc}"


# ── Skill ──────────────────────────────────────────────────────────────────────


class CommandWithFallbackSkill(BaseSkill):
    name = "command_with_fallback"
    description = (
        "Execute a sequence of shell commands with automatic error recovery. "
        "Each step: ✅ success → continue, ❌ failure → parse error, create a task, "
        "attempt LLM-assisted auto-fix. "
        "Params: commands=[list], cwd=path, context=description, auto_fix=true/false. "
        "Use this for multi-step workflows (git ops, builds, deploys) where you need "
        "structured success/failure tracking."
    )
    trigger_intents = ["command_with_fallback"]
    approval_category = ApprovalCategory.NONE  # individual command safety checked per-step

    def is_available(self) -> bool:
        return True

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        # Normalise commands list
        raw_commands = params.get("commands") or []
        if isinstance(raw_commands, str):
            raw_commands = [raw_commands]
        single = (params.get("command") or "").strip()
        if single and single not in raw_commands:
            raw_commands = [single] + raw_commands
        if not raw_commands:
            return SkillResult(
                context_data=(
                    "[command_with_fallback requires 'commands' (list) or 'command' (str). "
                    "Provide an ordered list of shell commands to run.]"
                ),
                skill_name=self.name,
            )

        cwd = (params.get("cwd") or _DEFAULT_CWD).rstrip("/") or _DEFAULT_CWD
        chain_context = (params.get("context") or "").strip()
        auto_fix = str(params.get("auto_fix", "true")).lower() not in ("false", "0", "no")

        step_reports: list[str] = []
        steps_passed = 0
        failed_at: int | None = None

        for i, cmd in enumerate(raw_commands):
            cmd = cmd.strip()
            if not cmd:
                continue

            # Protected-path and forbidden checks
            check_targets = [cmd, cwd]
            if any(_touches_protected_path(t) for t in check_targets):
                step_reports.append(
                    f"**Step {i + 1}** — `{cmd}`\n"
                    "Status: ❌ Blocked — references a protected path (/root/sentinel, "
                    "/sentinel-project, or bare /sentinel). Use /root/sentinel-workspace."
                )
                failed_at = i
                break

            if _is_forbidden(cmd):
                step_reports.append(
                    f"**Step {i + 1}** — `{cmd}`\nStatus: ❌ Blocked — low-level disk operation not permitted."
                )
                failed_at = i
                break

            output, code = await _run_command(cmd, cwd)

            if code == 0:
                step_reports.append(_format_step(i, cmd, output, code))
                steps_passed += 1
            else:
                error_type = _classify_error(output)
                task_status = _create_failure_task(cmd, output, error_type, i, chain_context)
                fix_info = task_status

                if auto_fix:
                    fix_summary, _ = await _attempt_autofix(cmd, output, error_type, cwd, chain_context)
                    fix_info += f"\n{fix_summary}"

                step_reports.append(_format_step(i, cmd, output, code, fix_info))
                failed_at = i
                break  # stop chain on first failure

        # Summary header
        total = len([c for c in raw_commands if c.strip()])
        ran = steps_passed + (1 if failed_at is not None else 0)
        skipped = total - ran

        if failed_at is None:
            header = f"✅ **All {total} step(s) completed successfully.**"
        else:
            header = (
                f"❌ **Chain stopped at step {failed_at + 1} of {total}.**  "
                f"{steps_passed} step(s) passed before failure." + (f"  {skipped} step(s) skipped." if skipped else "")
            )

        divider = "─" * 48
        context_data = header + f"\n{divider}\n" + f"\n{divider}\n".join(step_reports)

        return SkillResult(context_data=context_data, skill_name=self.name)
