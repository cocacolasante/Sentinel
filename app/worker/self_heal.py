"""
Self-Heal Worker — autonomous error triage + patch pipeline.

When a Sentinel skill throws an unhandled exception the dispatcher fires this
Celery task, which:
  1. Creates a GitHub issue with full error context.
  2. Asks Haiku for a structured fix plan (root cause + file patch).
  3. If auto-fixable: reads the file, applies the patch, commits to a new
     sentinel/heal-* branch, pushes, and opens a PR.
  4. Posts a Slack summary to sentinel-alerts regardless of outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

_FIX_PLAN_SCHEMA = """{
  "fixable": true | false,
  "root_cause": "one-sentence root cause",
  "fix_description": "what the patch does",
  "file_path": "relative path from repo root e.g. app/skills/foo.py",
  "old_code": "exact unique substring to replace",
  "new_code": "replacement code"
}"""


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.self_heal.auto_heal_skill_error",
    queue="tasks_general",
    max_retries=0,
    soft_time_limit=300,
    time_limit=360,
)
def auto_heal_skill_error(
    self,
    skill_name: str,
    error_type: str,
    error_msg: str,
    tb: str,
    original_message: str,
    session_id: str,
) -> dict:
    try:
        return asyncio.run(
            _heal(skill_name, error_type, error_msg, tb, original_message, session_id)
        )
    except Exception as exc:
        logger.error("self_heal task crashed: %s", exc)
        return {"status": "crashed", "error": str(exc)}


# ── Core heal pipeline ────────────────────────────────────────────────────────


async def _heal(
    skill_name: str,
    error_type: str,
    error_msg: str,
    tb: str,
    original_message: str,
    session_id: str,
) -> dict:
    from app.config import get_settings
    from app.integrations.github import GitHubClient
    from app.integrations.slack_notifier import post_alert_sync

    s = get_settings()
    repo = s.github_default_repo
    gh = GitHubClient()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── 1. Open GitHub issue ──────────────────────────────────────────────────
    issue_url = ""
    issue_number: int | None = None
    if gh.is_configured() and repo:
        issue_body = (
            f"## 🤖 Autonomous error report — `{skill_name}`\n\n"
            f"**Error:** `{error_type}: {error_msg[:300]}`\n"
            f"**Time:** {now}\n"
            f"**Session:** `{session_id}`\n"
            f"**Triggered by:** _{original_message[:200]}_\n\n"
            f"### Traceback\n```\n{tb[:3000]}\n```\n\n"
            f"---\n*Sentinel is attempting an autonomous patch. A PR will follow if a fix is found.*"
        )
        try:
            created = await gh.create_issue(
                repo=repo,
                title=f"[Auto] {skill_name}: {error_type} — {error_msg[:80]}",
                body=issue_body,
                labels=["bug", "auto-triage"],
            )
            issue_url = created.get("url", "")
            issue_number = created.get("number")
            logger.info("self_heal: GitHub issue #%s created for %s", issue_number, skill_name)
        except Exception as exc:
            logger.warning("self_heal: could not create GitHub issue: %s", exc)

    # ── 2. LLM fix plan ───────────────────────────────────────────────────────
    fix_plan: dict = {}
    if s.anthropic_api_key:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=s.anthropic_api_key)
            prompt = (
                f"A Sentinel AI skill threw an unhandled exception.\n\n"
                f"Skill: {skill_name}\n"
                f"Error: {error_type}: {error_msg}\n\n"
                f"Traceback:\n{tb[:2000]}\n\n"
                f"User message that triggered it: {original_message[:300]}\n\n"
                f"Produce a structured fix plan as JSON:\n{_FIX_PLAN_SCHEMA}\n\n"
                "Rules:\n"
                "- Set fixable=false if the error is environmental, config-missing, rate-limit, or network.\n"
                "- file_path must be a Python source file inside app/ or sentinel-agent/.\n"
                "- old_code must be an exact unique substring that appears verbatim in the file.\n"
                "- If unsure, set fixable=false.\n"
                "Return ONLY valid JSON — no markdown fences."
            )
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.lower().startswith("json"):
                    raw = raw[4:]
            fix_plan = json.loads(raw.strip())
        except Exception as exc:
            logger.warning("self_heal: LLM fix plan failed: %s", exc)
            fix_plan = {"fixable": False, "root_cause": "LLM analysis failed"}

    root_cause = fix_plan.get("root_cause", "Unknown")
    fixable = bool(fix_plan.get("fixable", False))

    # ── 3. Apply patch + push PR ──────────────────────────────────────────────
    pr_url = ""
    if (
        fixable
        and fix_plan.get("file_path")
        and fix_plan.get("old_code")
        and fix_plan.get("new_code")
    ):
        try:
            pr_url = await _apply_patch_and_pr(fix_plan, skill_name, issue_number, repo)
        except Exception as exc:
            logger.error("self_heal: patch pipeline failed: %s", exc)
            fixable = False

    # ── 4. Slack summary ──────────────────────────────────────────────────────
    icon = "🔧" if pr_url else ("⚠️" if not fixable else "📋")
    lines = [
        f"{icon} *Self-Heal triggered — `{skill_name}`*",
        f"*Error:* `{error_type}: {error_msg[:120]}`",
        f"*Root cause:* {root_cause}",
    ]
    if issue_url:
        lines.append(f"*GitHub issue:* {issue_url}")
    if pr_url:
        lines.append(f"*Auto-fix PR:* {pr_url} ✅")
    elif fixable:
        lines.append("_Patch attempted but push failed — check logs._")
    else:
        lines.append("_Not auto-fixable — manual review needed._")

    try:
        post_alert_sync("\n".join(lines), s.slack_alert_channel)
    except Exception:
        pass

    return {
        "skill": skill_name,
        "issue_url": issue_url,
        "pr_url": pr_url,
        "fixable": fixable,
        "root_cause": root_cause,
    }


# ── Patch + PR helper ─────────────────────────────────────────────────────────


async def _apply_patch_and_pr(
    fix_plan: dict,
    skill_name: str,
    issue_number: int | None,
    repo: str,
) -> str:
    """Apply fix_plan patch to the workspace, commit to a new branch, push, open PR."""
    from app.integrations.github import GitHubClient

    code_root = "/root/sentinel-workspace" if os.path.isdir("/root/sentinel-workspace") else "/app"
    file_rel = fix_plan["file_path"].lstrip("/")
    file_abs = os.path.join(code_root, file_rel)

    # Read source
    with open(file_abs, "r") as fh:
        content = fh.read()

    old = fix_plan["old_code"]
    new = fix_plan["new_code"]
    if old not in content:
        raise RuntimeError(f"old_code not found verbatim in {file_rel}")

    patched = content.replace(old, new, 1)
    with open(file_abs, "w") as fh:
        fh.write(patched)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"sentinel/heal-{skill_name.replace('_', '-')}-{ts}"
    issue_ref = f" (closes #{issue_number})" if issue_number else ""
    commit_msg = (
        f"fix(auto-heal): {skill_name} — "
        f"{fix_plan.get('fix_description', 'auto patch')[:80]}"
        f"{issue_ref}"
    )

    cmds = (
        f"cd {code_root} && "
        f"git checkout -B {branch} origin/main && "
        f"git add {file_rel} && "
        f"git commit -m '{commit_msg}' && "
        f"git push origin {branch}"
    )
    proc = await asyncio.create_subprocess_shell(
        cmds,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"git push failed: {err}")

    # Open PR
    gh = GitHubClient()
    pr_body = (
        f"## Auto-fix: `{skill_name}`\n\n"
        f"**Root cause:** {fix_plan.get('root_cause', '')}\n\n"
        f"**Fix:** {fix_plan.get('fix_description', '')}\n\n"
        + (f"Closes #{issue_number}\n\n" if issue_number else "")
        + "_Generated autonomously by Sentinel self-heal pipeline._"
    )
    try:
        pr = await gh.create_pr(
            repo=repo,
            title=f"[Auto-fix] {skill_name}: {fix_plan.get('fix_description', '')[:60]}",
            body=pr_body,
            head=branch,
            base="main",
        )
        return pr.get("url", "")
    except Exception as exc:
        logger.warning("self_heal: GitHub PR API failed (%s), trying gh CLI", exc)
        pr_proc = await asyncio.create_subprocess_shell(
            f"cd {code_root} && gh pr create "
            f"--title 'Auto-fix: {skill_name}' "
            f"--body 'Sentinel self-heal' "
            f"--base main --head {branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(pr_proc.communicate(), timeout=30)
        return out.decode().strip()
