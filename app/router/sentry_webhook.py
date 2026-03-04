"""
Sentry Webhook Router

Receives Sentry alert webhooks and converts them into Brain pending_write_tasks
based on issue severity, following the existing ApprovalCategory system:

  fatal    → BREAKING  (always requires confirmation)
  critical → CRITICAL  (confirm at approval levels 1 & 2)
  error    → CRITICAL  (confirm at approval levels 1 & 2)
  warning  → STANDARD  (confirm at approval level 1 only)
  info     → NONE      (logged to sentry_issues table, no task created)
  debug    → NONE      (logged to sentry_issues table, no task created)

Configure in Sentry:
  Settings → Integrations → Webhooks → Add Internal Integration
  Webhook URL: https://your-domain.com/api/v1/sentry/webhook
  Check "issue" events

Optional signing secret:
  Set SENTRY_WEBHOOK_SECRET in .env, then add it to your Sentry integration.

Approval dashboard: GET /api/v1/approval/pending
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from loguru import logger
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings

settings = get_settings()

router = APIRouter(prefix="/sentry", tags=["sentry"])

# Sentry level → (approval_category, task_priority)
_LEVEL_MAP: dict[str, tuple[str, str]] = {
    "fatal":    ("breaking",  "urgent"),
    "critical": ("critical",  "urgent"),
    "error":    ("critical",  "high"),
    "warning":  ("standard",  "normal"),
    "info":     ("none",      "low"),
    "debug":    ("none",      "low"),
}


def _verify_signature(payload: bytes, signature: str | None) -> bool:
    """Verify Sentry HMAC-SHA256 webhook signature if secret is configured."""
    secret = getattr(settings, "sentry_webhook_secret", "")
    if not secret:
        return True   # No secret configured — accept all
    if not signature:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _save_sentry_issue(
    issue_id:   str,
    title:      str,
    level:      str,
    status:     str,
    project:    str,
    permalink:  str,
    count:      int,
    platform:   str,
    first_seen: str,
    category:   str,
) -> None:
    try:
        from app.db import postgres
        postgres.execute(
            """
            INSERT INTO sentry_issues
                   (issue_id, title, level, status, project, permalink,
                    count, platform, first_seen, category, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (issue_id) DO UPDATE
               SET status      = EXCLUDED.status,
                   count       = EXCLUDED.count,
                   received_at = NOW()
            """,
            (issue_id, title, level, status, project, permalink,
             count, platform, first_seen, category),
        )
    except Exception as exc:
        logger.warning("Could not save sentry_issue row: {}", exc)


def _create_pending_task(task_id: str, title: str, params: dict, category: str) -> None:
    try:
        from app.db import postgres
        postgres.execute(
            """
            INSERT INTO pending_write_tasks
                   (task_id, session_id, action, title, params, category, status)
            VALUES (%s, 'sentry-webhook', 'sentry_investigate', %s, %s, %s, 'awaiting_approval')
            ON CONFLICT DO NOTHING
            """,
            (task_id, title, json.dumps(params), category),
        )
        logger.info(
            "Sentry task created | task_id={} | category={} | title={}",
            task_id, category, title[:60],
        )
    except Exception as exc:
        logger.error("Could not create sentry pending task: {}", exc)


_LEVEL_BADGE = {
    "fatal":    "🔴",
    "critical": "🔴",
    "error":    "🟠",
    "warning":  "🟡",
}


async def _maybe_slack_alert(
    level: str,
    title: str,
    project: str,
    permalink: str,
    count: int,
    issue_id: str,
) -> None:
    """Post a Slack alert for critical/fatal Sentry issues."""
    if level not in ("fatal", "critical", "error"):
        return
    try:
        from app.integrations.slack_notifier import post_alert
        badge = _LEVEL_BADGE.get(level, "🟠")
        count_str = f"{count:,}" if count else "?"
        link_text = f"<{permalink}|View in Sentry>" if permalink else f"Issue `{issue_id}`"
        text = (
            f"{badge} *Sentry {level.upper()} — {project}*\n"
            f"*{title}*\n"
            f"Occurrences: *{count_str}*  ·  {link_text}\n"
            f"_Brain approval task created — check the dashboard to review._"
        )
        await post_alert(text)
    except Exception as exc:
        logger.warning("Could not post Sentry Slack alert: {}", exc)


async def _process_sentry_event(payload: dict) -> None:
    """Parse a Sentry webhook payload and create a Brain task if warranted."""
    try:
        action = payload.get("action", "created")
        issue  = payload.get("data", {}).get("issue") or payload.get("issue") or {}

        if not issue:
            logger.debug("Sentry webhook: no issue payload — skipping")
            return

        issue_id   = str(issue.get("id", ""))
        title      = issue.get("title", "Unknown error")
        level      = (issue.get("level") or "error").lower()
        status     = issue.get("status", "unresolved")
        proj_raw   = issue.get("project", {})
        project    = proj_raw.get("slug", "") if isinstance(proj_raw, dict) else str(proj_raw)
        permalink  = issue.get("permalink", "")
        count      = int(issue.get("count") or 0)
        platform   = issue.get("platform", "")
        first_seen = issue.get("firstSeen", "")

        logger.info(
            "Sentry webhook | action={} | level={} | issue={} | title={}",
            action, level, issue_id, title[:80],
        )

        # Skip resolution events — nothing to action
        if action == "resolved" or status == "resolved":
            logger.debug("Sentry webhook: issue already resolved, skipping task creation")
            return

        category, _priority = _LEVEL_MAP.get(level, ("critical", "high"))

        # Always persist to the sentry_issues audit table
        _save_sentry_issue(issue_id, title, level, status, project, permalink,
                           count, platform, first_seen, category)

        # info / debug → log only, no pending task, no alert
        if category == "none":
            return

        task_id    = str(uuid.uuid4())
        task_title = f"[Sentry {level.upper()}] {title[:120]}"
        # Extract affected app/ filenames from the webhook's inline stack trace
        _affected: list[str] = []
        for entry in issue.get("entries", []):
            if entry.get("type") != "exception":
                continue
            for exc_val in (entry.get("data") or {}).get("values", []):
                for frame in (exc_val.get("stacktrace") or {}).get("frames", []):
                    fn = frame.get("filename", "")
                    if fn.startswith("app/") and fn not in _affected:
                        _affected.append(fn)

        task_params = {
            "issue_id":       issue_id,
            "title":          title,
            "level":          level,
            "project":        project,
            "permalink":      permalink,
            "count":          count,
            "platform":       platform,
            "first_seen":     first_seen,
            "affected_files": _affected,
        }

        _create_pending_task(task_id, task_title, task_params, category)

        # Dispatch auto-investigation + fix in a Celery worker (non-blocking)
        try:
            from app.worker.tasks import investigate_and_fix_sentry_issue
            investigate_and_fix_sentry_issue.delay(task_id, task_params)
            logger.info("Dispatched auto-fix task | task_id={} | issue={}", task_id, issue_id)
        except Exception as exc:
            logger.warning("Could not dispatch auto-fix Celery task: {}", exc)

        # Alert Slack for critical/fatal issues
        await _maybe_slack_alert(level, title, project, permalink, count, issue_id)

    except Exception as exc:
        logger.error("Sentry webhook processing error: {}", exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/webhook")
async def sentry_webhook(
    request:    Request,
    background: BackgroundTasks,
    sentry_hook_signature: str | None = Header(None, alias="sentry-hook-signature"),
):
    """
    Receive Sentry issue webhook events and spin them into Brain approval tasks.

    Severity mapping:
      fatal / critical / error → CRITICAL task (awaiting_approval)
      warning                  → STANDARD task (awaiting_approval)
      info / debug             → logged only, no task

    Review pending tasks at: GET /api/v1/approval/pending
    """
    body = await request.body()

    if not _verify_signature(body, sentry_hook_signature):
        logger.warning("Sentry webhook: signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    background.add_task(_process_sentry_event, payload)
    return JSONResponse({"ok": True})


@router.get("/issues")
async def list_tracked_issues(limit: int = 25):
    """List recent Sentry issues tracked in the Brain's database."""
    try:
        from app.db import postgres
        rows = postgres.execute(
            """
            SELECT issue_id, title, level, status, project, count,
                   permalink, category, received_at
            FROM   sentry_issues
            ORDER  BY received_at DESC
            LIMIT  %s
            """,
            (limit,),
        )
        return {
            "issues": [
                {
                    "issue_id":    r["issue_id"],
                    "title":       r["title"],
                    "level":       r["level"],
                    "status":      r["status"],
                    "project":     r["project"],
                    "count":       r["count"],
                    "permalink":   r["permalink"],
                    "category":    r["category"],
                    "received_at": r["received_at"].isoformat() if r.get("received_at") else None,
                }
                for r in rows
            ]
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
