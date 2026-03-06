"""
Task lifecycle notifications to the sentinel-tasks Slack channel.

Each task gets a parent message posted when it is created.
All subsequent status updates and the final report are posted as thread replies.

Functions ending in _sync are safe to call from synchronous Celery code.
Async wrappers are available for use inside FastAPI route handlers and async skills.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_PRIORITY_LABEL = {
    1: ":large_green_circle: Low",
    2: ":large_blue_circle: Minor",
    3: ":large_yellow_circle: Normal",
    4: ":large_orange_circle: High",
    5: ":red_circle: Critical",
}
_APPROVAL_LABEL = {
    1: "auto-approve",
    2: "needs review",
    3: "requires sign-off",
}
_STATUS_EMOJI = {
    "pending": ":hourglass_flowing_sand:",
    "in_progress": ":arrows_counterclockwise:",
    "done": ":white_check_mark:",
    "failed": ":x:",
    "cancelled": ":no_entry_sign:",
    "archived": ":package:",
}


def _channel() -> str:
    from app.config import get_settings

    return get_settings().slack_tasks_channel or "sentinel-tasks"


def _make_client():
    from app.config import get_settings
    from slack_sdk import WebClient

    return WebClient(token=get_settings().slack_bot_token)


# ── DB helpers ─────────────────────────────────────────────────────────────────


def get_task_slack_ts(task_id: int) -> Optional[str]:
    """Return the sentinel-tasks thread_ts stored for this task, or None."""
    try:
        from app.db import postgres

        row = postgres.execute_one(
            "SELECT task_slack_ts, title FROM tasks WHERE id = %s", (task_id,)
        )
        return (row or {}).get("task_slack_ts")
    except Exception as exc:
        logger.warning("get_task_slack_ts(%s) failed: %s", task_id, exc)
        return None


def store_task_slack_ts(task_id: int, ts: str) -> None:
    """Persist the sentinel-tasks thread_ts for a task."""
    try:
        from app.db import postgres

        postgres.execute(
            "UPDATE tasks SET task_slack_ts = %s WHERE id = %s", (ts, task_id)
        )
    except Exception as exc:
        logger.warning("store_task_slack_ts(%s) failed: %s", task_id, exc)


def _get_task_title(task_id: int) -> str:
    """Fetch the task title from DB (used by _mark_task which has no title)."""
    try:
        from app.db import postgres

        row = postgres.execute_one("SELECT title FROM tasks WHERE id = %s", (task_id,))
        return (row or {}).get("title") or f"Task #{task_id}"
    except Exception:
        return f"Task #{task_id}"


# ── Core posting helpers ───────────────────────────────────────────────────────


def post_task_created_sync(
    task_id: int,
    title: str,
    priority_num: int = 3,
    approval_level: int = 1,
    description: str = "",
    source: str = "brain",
) -> Optional[str]:
    """
    Post a task card to the sentinel-tasks channel.
    Returns the Slack message ts (thread anchor) or None on failure.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.slack_bot_token:
        return None

    pri = _PRIORITY_LABEL.get(priority_num, str(priority_num))
    app = _APPROVAL_LABEL.get(approval_level, str(approval_level))
    text = (
        f":clipboard: *Task #{task_id} — {title}*\n"
        f"Status: :hourglass_flowing_sand: pending  |  Priority: {pri}  |  Approval: {app}\n"
        f"Source: `{source}`"
    )
    if description:
        short = description[:300] + ("..." if len(description) > 300 else "")
        text += f"\n>{short}"

    try:
        resp = _make_client().chat_postMessage(
            channel=_channel(),
            text=text,
            mrkdwn=True,
        )
        if resp.get("ok"):
            ts = resp["ts"]
            store_task_slack_ts(task_id, ts)
            logger.info("Task #%s posted to sentinel-tasks ts=%s", task_id, ts)
            return ts
        logger.error("post_task_created_sync failed: %s", resp.get("error"))
    except Exception as exc:
        logger.error("post_task_created_sync exception: %s", exc)
    return None


def _post_thread_reply_sync(task_id: int, text: str) -> bool:
    """Reply to the task's sentinel-tasks thread. Returns True on success."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.slack_bot_token:
        return False

    ts = get_task_slack_ts(task_id)
    if not ts:
        logger.warning("No task_slack_ts for task #%s — cannot post thread reply", task_id)
        return False

    try:
        resp = _make_client().chat_postMessage(
            channel=_channel(),
            thread_ts=ts,
            text=text,
            mrkdwn=True,
        )
        return bool(resp.get("ok"))
    except Exception as exc:
        logger.error("_post_thread_reply_sync(%s) exception: %s", task_id, exc)
        return False


# ── Public lifecycle notifications ─────────────────────────────────────────────


def notify_status_sync(
    task_id: int,
    title: str,
    new_status: str,
    extra: str = "",
) -> bool:
    """Post a status-change reply to the task's sentinel-tasks thread."""
    emoji = _STATUS_EMOJI.get(new_status, "ℹ️")
    text = f"{emoji} *Task #{task_id} — {title}* is now `{new_status}`"
    if extra:
        text += f"\n{extra}"
    return _post_thread_reply_sync(task_id, text)


def notify_report_sync(
    task_id: int,
    title: str,
    passed: bool,
    body: str = "",
    pr_url: str = "",
) -> bool:
    """Post the final execution report to the task's sentinel-tasks thread."""
    header = (
        f":white_check_mark: *Task #{task_id} — {title}* — *complete*"
        if passed
        else f":x: *Task #{task_id} — {title}* — *failed*"
    )
    divider = "─" * 36
    parts = [header]
    if body:
        # Trim very long reports — Slack has a 3000-char limit per block
        parts.append(body[:2800] + ("..." if len(body) > 2800 else ""))
    if pr_url:
        parts.append(f":twisted_rightwards_arrows: PR: {pr_url}")
    return _post_thread_reply_sync(task_id, f"\n{divider}\n".join(parts))


# ── Async wrappers for FastAPI / async skill contexts ──────────────────────────


async def post_task_created(
    task_id: int,
    title: str,
    priority_num: int = 3,
    approval_level: int = 1,
    description: str = "",
    source: str = "brain",
) -> Optional[str]:
    import asyncio

    return await asyncio.to_thread(
        post_task_created_sync,
        task_id,
        title,
        priority_num,
        approval_level,
        description,
        source,
    )


async def notify_status(
    task_id: int,
    title: str,
    new_status: str,
    extra: str = "",
) -> bool:
    import asyncio

    return await asyncio.to_thread(notify_status_sync, task_id, title, new_status, extra)


async def notify_report(
    task_id: int,
    title: str,
    passed: bool,
    body: str = "",
    pr_url: str = "",
) -> bool:
    import asyncio

    return await asyncio.to_thread(notify_report_sync, task_id, title, passed, body, pr_url)
