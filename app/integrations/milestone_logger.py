"""
Milestone Logger — records every AI-initiated write action.

Logs to:
  1. ai_milestones DB table (persistent audit trail)
  2. #sentinel-milestones Slack channel (real-time notifications)

Called from:
  - Dispatcher._fire_milestone() after _execute_pending() succeeds
  - ServerShellSkill for docker_restart / docker_compose / write-type commands
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Action label map ──────────────────────────────────────────────────────────
_ACTION_LABELS: dict[str, tuple[str, str]] = {
    "write_file": ("🔧", "Code Write"),
    "patch_file": ("🔧", "Code Patch"),
    "commit": ("📦", "Git Commit"),
    "push": ("📦", "Git Push"),
    "commit_push": ("📦", "Git Commit & Push"),
    "shell_exec": ("💻", "Shell Exec"),
    "deploy_brain": ("🚀", "Brain Deploy"),
    "send_email": ("📧", "Email Sent"),
    "reply_email": ("📧", "Email Reply"),
    "create_calendar_event": ("📅", "Calendar Event"),
    "add_contact": ("👤", "Contact Added"),
    "update_contact": ("👤", "Contact Updated"),
    "delete_contact": ("👤", "Contact Deleted"),
    "send_whatsapp": ("📱", "WhatsApp Sent"),
    "trigger_workflow": ("⚙️", "Workflow Triggered"),
    "docker_restart": ("🐳", "Docker Restart"),
    "docker_compose": ("🐳", "Docker Compose"),
    "task_update": ("📋", "Task Updated"),
    "sentry_resolve": ("🐛", "Sentry Resolved"),
    "sentry_ignore": ("🐛", "Sentry Ignored"),
    "sentry_assign": ("🐛", "Sentry Assigned"),
    "sentry_comment": ("🐛", "Sentry Note Added"),
    "sentry_investigate": ("🐛", "Sentry Investigated"),
}


def _get_label(action: str, intent: str) -> tuple[str, str]:
    if action in _ACTION_LABELS:
        return _ACTION_LABELS[action]
    if intent == "ionos_cloud":
        return ("☁️", "Cloud Operation")
    if intent == "ionos_dns":
        return ("🌐", "DNS Change")
    if intent == "n8n_manage":
        return ("⚙️", "n8n Change")
    if action.startswith("sentry_"):
        return ("🐛", "Sentry Action")
    return ("🤖", "AI Action")


def _build_summary(params: dict) -> str:
    """Extract the most meaningful fields from params for a one-line summary."""
    priority_keys = ("path", "message", "to", "subject", "name", "service", "command", "reason", "workflow_id", "title")
    parts = []
    for key in priority_keys:
        val = params.get(key)
        if val and isinstance(val, str) and val.strip():
            short = val.strip()[:80]
            parts.append(f"`{key}`: {short}")
            if len(parts) == 3:
                break
    return " · ".join(parts)


async def log_milestone(
    action: str,
    intent: str,
    params: dict,
    session_id: str,
    original: str = "",
    agent: str = "",
    detail: dict | None = None,
) -> None:
    """
    Persist an AI action milestone to the database and post to #sentinel-milestones.

    Safe to call with asyncio.create_task() — all errors are swallowed so this
    never disrupts the main conversation flow.
    """
    from app.config import get_settings

    settings = get_settings()

    emoji, label = _get_label(action, intent)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = _build_summary(params) or action

    # ── 1. Database ───────────────────────────────────────────────────────────
    try:
        from app.db import postgres

        postgres.execute(
            """
            INSERT INTO ai_milestones
                   (session_id, action, intent, summary, detail, agent)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                action,
                intent,
                summary[:500],
                json.dumps(detail if detail is not None else params),
                agent or "brain",
            ),
        )
        logger.debug("Milestone logged to DB | action=%s | session=%s", action, session_id)
    except Exception as exc:
        logger.warning("Milestone DB write failed: %s", exc)

    # ── 2. Slack ──────────────────────────────────────────────────────────────
    channel = getattr(settings, "slack_milestone_channel", "sentinel-milestones")
    lines = [f"{emoji} *{label}* — `{action}`"]
    if summary and summary != action:
        lines.append(f"> {summary}")
    meta = f"`{session_id}`"
    if agent:
        meta += f" · agent: `{agent}`"
    lines.append(f"> Session: {meta}")
    lines.append(f"> {ts}")
    text = "\n".join(lines)

    try:
        from app.integrations.slack_notifier import post_alert

        await post_alert(text, channel=channel)
        logger.debug("Milestone posted to Slack | channel=%s | action=%s", channel, action)
    except Exception as exc:
        logger.warning("Milestone Slack post failed: %s", exc)
