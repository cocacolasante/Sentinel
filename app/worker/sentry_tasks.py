"""
Sentry Ingestion & Triage Tasks

ingest_and_triage_top_errors — scheduled every 6 hours via Beat.
  1. Fetch top 10 most-frequent unresolved errors from the past 6 hours.
  2. For each error, upsert a board task in the `tasks` table (skip if a
     sentry-triage task for the same issue already exists and is not done).
  3. Dispatch `investigate_and_fix_sentry_issue` for each new task.
  4. Post a Slack summary to brain-alerts.
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
    create a board task for each, and kick off the investigate-and-fix pipeline.
    """
    import asyncio
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
            query="is:unresolved",
            limit=limit,
            sort="freq",
            stats_period=stats_period,
        )
    except Exception as exc:
        logger.error("Failed to fetch Sentry issues: %s", exc, exc_info=True)
        return {"error": str(exc), "tasks_created": 0}

    if not issues:
        logger.info("No unresolved Sentry issues found for period=%s", stats_period)
        return {"tasks_created": 0, "issues_fetched": 0}

    # ── 2. Create board tasks ──────────────────────────────────────────────────
    created_ids: list[int] = []
    skipped: int = 0

    for rank, issue in enumerate(issues[:limit], start=1):
        issue_id = issue.get("id", "")
        title = issue.get("title", "Unknown error")
        level = issue.get("level", "error")
        project = issue.get("project", "")
        count = issue.get("count", 0)
        permalink = issue.get("permalink", "")
        culprit = issue.get("culprit", "")

        task_title = f"[Sentry #{rank}] {title[:120]}"
        description = (
            f"Sentry issue ID: {issue_id}\n"
            f"Level: {level} | Project: {project} | Count: {count}\n"
            f"Culprit: {culprit}\n"
            f"Link: {permalink}\n\n"
            "Auto-created by Sentinel sentry-triage. "
            "Investigate root cause and apply a fix if possible."
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

        # Skip if an active triage task already exists for this Sentry issue_id
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
            logger.warning("Could not check for existing task for %s: %s", issue_id, exc)
            existing = None

        if existing:
            logger.info(
                "Sentry issue %s already has active task #%s (%s) — skipping",
                issue_id,
                existing["id"],
                existing["status"],
            )
            skipped += 1
            continue

        try:
            row = postgres.execute_one(
                """
                INSERT INTO tasks
                    (title, description, status, priority, priority_num, approval_level,
                     source, tags)
                VALUES (%s, %s, 'pending', %s, %s, 1, 'sentry-triage', %s::jsonb)
                RETURNING id
                """,
                (
                    task_title,
                    description,
                    "high" if level in ("fatal", "critical", "error") else "medium",
                    4 if level in ("fatal", "critical") else 3,
                    tags,
                ),
            )
        except Exception as exc:
            logger.error("Failed to insert task for Sentry issue %s: %s", issue_id, exc)
            continue

        task_id: int = row["id"]
        created_ids.append(task_id)

        # ── 3. Dispatch investigate-and-fix ───────────────────────────────────
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
            logger.info(
                "Dispatched investigate_and_fix for Sentry issue %s → task #%s",
                issue_id,
                task_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to dispatch fix task for Sentry issue %s: %s", issue_id, exc
            )

    # ── 4. Slack summary ──────────────────────────────────────────────────────
    if created_ids or skipped:
        lines = [
            f"🔍 *Sentry triage — top {limit} errors (last {stats_period})*",
            f"Created {len(created_ids)} new task(s){f', {skipped} already tracked' if skipped else ''}.",
        ]
        for rank, issue in enumerate(issues[:limit], start=1):
            level = issue.get("level", "error")
            badge = "🔴" if level in ("fatal", "critical") else "🟠" if level == "error" else "🟡"
            count = issue.get("count", 0)
            lines.append(
                f"{badge} #{rank} ({count}x) {issue.get('title', '')[:80]}"
            )
        try:
            post_alert_sync("\n".join(lines))
        except Exception as exc:
            logger.warning("Could not post Slack summary: %s", exc)

    return {
        "issues_fetched": len(issues),
        "tasks_created": len(created_ids),
        "tasks_skipped": skipped,
        "task_ids": created_ids,
    }
