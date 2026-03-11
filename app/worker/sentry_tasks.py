"""
Sentry Ingestion & Triage Tasks

ingest_and_triage_top_errors — runs 4x daily at 12am/6am/12pm/6pm UTC.
  1. Fetch top 10 most-frequent unresolved errors from the past 6 hours.
  2. For each error, create a board task (approval_level=1 — auto-starts immediately).
  3. Skip errors that already have an active triage task.
  4. Dispatch investigate_and_fix_sentry_issue for each new task.
  5. Post a Slack run summary to sentinel-alerts.

Each investigate_and_fix task posts its own Slack updates:
  - On start: "🧠 Investigating…"
  - On completion: "✅ Fix pushed — PR opened" or "🐛 GitHub issue created/updated (not auto-fixable)"
"""

from __future__ import annotations

import json
import logging
import re

from celery import shared_task

logger = logging.getLogger(__name__)

# Strip log-line prefixes emitted by loguru/stdlib:
#   "2026-03-05 13:39:38.174 | ERROR | module:method:57 - actual message"
_LOG_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]\d+\s*\|\s*\w+\s*\|\s*[^\|]+ - ",
)

# Dynamic tokens that vary between occurrences of the same error type:
# Slack session IDs (s_17592004491665), UUIDs, long numeric IDs, hex addresses,
# ISO timestamps, short date/time components.
_DYNAMIC_TOKEN_RE = re.compile(
    r"\b(s_[0-9a-f]+|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"0x[0-9a-f]+|"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d*|"  # ISO timestamps
    r"\d{8,})\b",                                          # long numeric IDs
    re.IGNORECASE,
)


def _error_fingerprint(title: str, culprit: str) -> str:
    """
    Stable key for grouping Sentry issues that represent the same error type.
    Strips log-line prefixes (timestamps, log levels, logger names) and dynamic
    tokens (session IDs, UUIDs, long numerics) then combines with culprit so
    different callsites stay separate.
    """
    # Remove "2026-03-05 13:39:38.174 | ERROR | module:method:57 - " prefix
    normalized = _LOG_PREFIX_RE.sub("", title)
    normalized = _DYNAMIC_TOKEN_RE.sub("*", normalized)
    # Collapse parenthesised groups that contained a dynamic token
    normalized = re.sub(r"\(\*[^)]*\)", "(*)", normalized)
    return f"{normalized.strip()}|{culprit}"


@shared_task(
    name="app.worker.sentry_tasks.ingest_and_triage_top_errors",
    bind=False,
    max_retries=1,
    default_retry_delay=120,
    soft_time_limit=300,
    time_limit=360,
)
def ingest_and_triage_top_errors(stats_period: str = "6h", limit: int = 10) -> dict:
    """
    Fetch the top N most-frequent Sentry errors in the last `stats_period`,
    create a board task for each (no approval needed), and auto-dispatch
    the investigate-and-fix pipeline for each one.
    """
    from app.integrations.sentry_client import SentryClient
    from app.db import postgres
    from app.integrations.slack_notifier import post_alert_sync

    client = SentryClient()
    if not client.is_configured():
        logger.warning("Sentry not configured — skipping triage")
        return {"skipped": True, "reason": "Sentry not configured"}

    # ── 1. Fetch top errors by frequency in the stats window ──────────────────
    try:
        issues = client._list_issues_sync(
            project=None,
            query="is:unresolved",
            limit=limit,
            sort="freq",
            stats_period=stats_period,
        )
    except Exception as exc:
        logger.error("Failed to fetch Sentry issues: %s", exc, exc_info=True)
        post_alert_sync(f"❌ *Sentry triage failed* — could not fetch issues\n`{exc}`")
        return {"error": str(exc), "tasks_created": 0}

    if not issues:
        logger.info("No unresolved Sentry issues found for period=%s", stats_period)
        return {"tasks_created": 0, "issues_fetched": 0}

    # ── 2. Create board tasks and dispatch ────────────────────────────────────
    created: list[dict] = []   # [{task_id, rank, issue}]
    skipped: int = 0
    # Fingerprints seen in this run — prevents creating duplicate tasks when
    # Sentry lists multiple issues that are really the same error type
    # (e.g. same exception with different session IDs in the message).
    seen_fingerprints: set[str] = set()

    for rank, issue in enumerate(issues[:limit], start=1):
        issue_id = issue.get("id", "")
        title = issue.get("title", "Unknown error")
        level = issue.get("level", "error")
        project = issue.get("project", "")
        count = issue.get("count", 0)
        permalink = issue.get("permalink", "")
        culprit = issue.get("culprit", "")

        # Deduplicate within this run by normalised error fingerprint
        fp = _error_fingerprint(title, culprit)
        if fp in seen_fingerprints:
            logger.info(
                "Sentry issue %s is a duplicate of an already-queued error type "
                "(fingerprint: %s) — skipping",
                issue_id, fp[:80],
            )
            skipped += 1
            continue
        seen_fingerprints.add(fp)

        # Skip if a triage task for this exact issue ID or same error fingerprint
        # was created in the last 24 hours (regardless of done/failed status —
        # prevents re-investigating the same Sentry issue every run).
        try:
            existing = postgres.execute_one(
                """
                SELECT id, status FROM tasks
                WHERE source = 'sentry-triage'
                  AND (
                    description LIKE %s
                    OR description LIKE %s
                  )
                  AND created_at >= NOW() - INTERVAL '24 hours'
                LIMIT 1
                """,
                (
                    f"%Sentry issue ID: {issue_id}%",
                    f"%fingerprint: {fp[:100]}%",
                ),
            )
        except Exception as exc:
            logger.warning("Could not check existing task for %s: %s", issue_id, exc)
            existing = None

        if existing:
            logger.info(
                "Sentry issue %s already triaged as task #%s (%s) within 24h — skipping",
                issue_id, existing["id"], existing["status"],
            )
            skipped += 1
            continue

        task_title = f"[Sentry #{rank}] {title[:120]}"
        description = (
            f"Sentry issue ID: {issue_id}\n"
            f"Level: {level} | Project: {project} | Count: {count}\n"
            f"Culprit: {culprit}\n"
            f"Link: {permalink}\n"
            f"fingerprint: {fp}\n\n"
            "Auto-created by Sentinel sentry-triage. "
            "Investigating root cause and applying a fix if possible."
        )
        issue_params = {
            "issue_id": issue_id,
            "title": title,
            "level": level,
            "project": project,
            "permalink": permalink,
            "culprit": culprit,
            "count": count,
            "rank": rank,
        }
        tags = json.dumps(["sentry-triage", f"sentry-{level}", project or "unknown"])
        priority = "high" if level in ("fatal", "critical", "error") else "medium"
        priority_num = 4 if level in ("fatal", "critical") else 3

        try:
            row = postgres.execute_one(
                """
                INSERT INTO tasks
                    (title, description, status, priority, priority_num,
                     approval_level, source, tags)
                VALUES (%s, %s, 'pending', %s, %s, 1, 'sentry-triage', %s::jsonb)
                RETURNING id
                """,
                (task_title, description, priority, priority_num, tags),
            )
        except Exception as exc:
            logger.error("Failed to insert task for Sentry issue %s: %s", issue_id, exc)
            continue

        task_id: int = row["id"]

        # Dispatch immediately — approval_level=1 means no human gate before start
        try:
            from app.worker.tasks import investigate_and_fix_sentry_issue

            investigate_and_fix_sentry_issue.apply_async(
                args=[task_id, issue_params],
                queue="tasks_general",
            )
            postgres.execute(
                "UPDATE tasks SET celery_task_id='dispatched' WHERE id=%s",
                (task_id,),
            )
            created.append({"task_id": task_id, "rank": rank, "issue": issue})
            logger.info(
                "Dispatched investigation for Sentry issue %s → task #%s",
                issue_id, task_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to dispatch fix task for Sentry issue %s: %s", issue_id, exc,
            )

    # ── 3. Slack run summary ──────────────────────────────────────────────────
    badge_map = {"fatal": "🔴", "critical": "🔴", "error": "🟠", "warning": "🟡"}

    if created or skipped:
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
        dedup_note = f", {skipped} skipped (duplicate or in progress)" if skipped else ""
        header = (
            f"🔍 *Sentry triage — {now_utc} — top {len(issues)} errors (last {stats_period})*\n"
            f"Started {len(created)} investigation(s){dedup_note}. Slack updates per fix below."
        )
        lines = [header, "─" * 36]
        for item in created:
            iss = item["issue"]
            lvl = iss.get("level", "error")
            badge = badge_map.get(lvl, "🟡")
            lines.append(
                f"{badge} #{item['rank']} · task #{item['task_id']} · "
                f"({iss.get('count', 0)}x) {iss.get('title', '')[:80]}"
            )
        for rank, iss in enumerate(issues[:limit], 1):
            if not any(c["rank"] == rank for c in created):
                lvl = iss.get("level", "error")
                badge = badge_map.get(lvl, "🟡")
                lines.append(
                    f"{badge} #{rank} · _(already tracked)_ · {iss.get('title', '')[:70]}"
                )
        try:
            post_alert_sync("\n".join(lines))
        except Exception as exc:
            logger.warning("Could not post triage summary to Slack: %s", exc)

    return {
        "issues_fetched": len(issues),
        "tasks_created": len(created),
        "tasks_skipped": skipped,
        "task_ids": [c["task_id"] for c in created],
    }
