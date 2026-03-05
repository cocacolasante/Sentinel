"""
Sentry Ingestion & Triage Tasks

ingest_and_triage_top_errors — runs 4x daily at 12am/6am/12pm/6pm UTC.
  1. Fetch top 10 most-frequent unresolved errors from the past 6 hours.
  2. For each error, create a board task (approval_level=1 — auto-starts immediately).
  3. Skip errors that already have an active triage task.
  4. Dispatch investigate_and_fix_sentry_issue for each new task.
  5. Post a Slack run summary to brain-alerts.

Each investigate_and_fix task posts its own Slack updates:
  - On start: "🧠 Investigating…"
  - On completion: "✅ Fix pushed — PR opened" or "🔍 Root cause identified (not auto-fixable)"
"""

from __future__ import annotations

import json
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


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

    for rank, issue in enumerate(issues[:limit], start=1):
        issue_id = issue.get("id", "")
        title = issue.get("title", "Unknown error")
        level = issue.get("level", "error")
        project = issue.get("project", "")
        count = issue.get("count", 0)
        permalink = issue.get("permalink", "")
        culprit = issue.get("culprit", "")

        # Skip if an active (non-terminal) triage task already exists for this issue
        try:
            existing = postgres.execute_one(
                """
                SELECT id, status FROM tasks
                WHERE source = 'sentry-triage'
                  AND description LIKE %s
                  AND status NOT IN ('done', 'failed')
                LIMIT 1
                """,
                (f"%Sentry issue ID: {issue_id}%",),
            )
        except Exception as exc:
            logger.warning("Could not check existing task for %s: %s", issue_id, exc)
            existing = None

        if existing:
            logger.info(
                "Sentry issue %s already has active task #%s (%s) — skipping",
                issue_id, existing["id"], existing["status"],
            )
            skipped += 1
            continue

        task_title = f"[Sentry #{rank}] {title[:120]}"
        description = (
            f"Sentry issue ID: {issue_id}\n"
            f"Level: {level} | Project: {project} | Count: {count}\n"
            f"Culprit: {culprit}\n"
            f"Link: {permalink}\n\n"
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
        header = (
            f"🔍 *Sentry triage — {now_utc} — top {len(issues)} errors (last {stats_period})*\n"
            f"Started {len(created)} investigation(s)"
            + (f", {skipped} already in progress" if skipped else "")
            + ". Slack updates per fix below."
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
