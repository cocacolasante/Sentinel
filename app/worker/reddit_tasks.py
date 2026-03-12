"""
Reddit Celery Tasks — hourly digest dispatcher

Runs every hour, checks all enabled schedules stored in Redis, and dispatches
digests for any schedule whose next cron tick falls within the current hour.

Also handles static schedule from config (settings.reddit_subreddits +
settings.reddit_schedule_hour).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

_REDIS_SCHEDULES_KEY = "sentinel:reddit:schedules"


# ── Celery task ───────────────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.reddit_tasks.dispatch_reddit_digests",
    queue="celery",
    max_retries=2,
    soft_time_limit=580,
    time_limit=600,
)
def dispatch_reddit_digests(self) -> dict:
    """Hourly dispatcher: send Reddit digests for any due schedules."""
    try:
        return asyncio.run(_dispatch_all())
    except Exception as exc:
        logger.error("dispatch_reddit_digests failed: %s", exc)
        raise self.retry(exc=exc)


# ── Core dispatcher ───────────────────────────────────────────────────────────


async def _dispatch_all() -> dict:
    from app.config import get_settings

    s = get_settings()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    schedules = _load_schedules()

    # Inject static config schedules as virtual entries
    if s.reddit_subreddits and s.reddit_schedule_hour >= 0:
        for sub in [x.strip() for x in s.reddit_subreddits.split(",") if x.strip()]:
            schedules.append(
                {
                    "id": f"static-{sub}",
                    "subreddit": sub,
                    "cron": f"0 {s.reddit_schedule_hour} * * *",
                    "channel": s.slack_reddit_channel,
                    "time_filter": "day",
                    "limit": 10,
                    "enabled": True,
                }
            )

    dispatched = 0
    skipped = 0
    errors: list[str] = []

    for schedule in schedules:
        if not schedule.get("enabled", True):
            skipped += 1
            continue

        cron_expr = schedule.get("cron", "")
        if not _is_due(cron_expr, window_start, now):
            skipped += 1
            continue

        try:
            await _run_digest(schedule)
            dispatched += 1
            # Log milestone so Grafana can track digest activity
            try:
                from app.integrations.milestone_logger import log_milestone

                await log_milestone(
                    action="reddit_digest",
                    intent="reddit_read",
                    params={
                        "subreddit": schedule.get("subreddit", ""),
                        "channel": schedule.get("channel", ""),
                        "cron": schedule.get("cron", ""),
                        "schedule_id": schedule.get("id", ""),
                    },
                    session_id="celery-reddit",
                    original="",
                    agent="celery",
                )
            except Exception as _ms_exc:
                logger.debug("Reddit milestone log skipped: %s", _ms_exc)
        except Exception as exc:
            logger.error(
                "Reddit digest failed for r/%s: %s",
                schedule.get("subreddit"),
                exc,
            )
            errors.append(f"r/{schedule.get('subreddit')}: {exc}")

    logger.info(
        "Reddit digest run: dispatched=%d skipped=%d errors=%d",
        dispatched, skipped, len(errors),
    )
    return {"dispatched": dispatched, "skipped": skipped, "errors": errors}


async def _run_digest(schedule: dict) -> None:
    from app.integrations.reddit_client import (
        RedditClient,
        SubredditNotFoundError,
        SubredditPrivateError,
        RedditRateLimitError,
    )
    from app.integrations.slack_notifier import post_alert_sync
    from app.config import get_settings
    from app.skills.reddit_skill import RedditReadSkill

    s = get_settings()
    subreddit = schedule["subreddit"]
    channel = schedule.get("channel") or s.slack_reddit_channel
    time_filter = schedule.get("time_filter", "day")
    limit = int(schedule.get("limit", 10))

    client = RedditClient()
    try:
        top_posts = await client.fetch_top_posts(subreddit, limit=limit, time_filter=time_filter)
        hot_posts = await client.fetch_hot_posts(subreddit, limit=5)
    except SubredditNotFoundError as exc:
        post_alert_sync(f"❌ Reddit digest error — r/{subreddit}: {exc}", channel)
        return
    except SubredditPrivateError as exc:
        post_alert_sync(f"🔒 Reddit digest error — r/{subreddit}: {exc}", channel)
        return
    except RedditRateLimitError as exc:
        post_alert_sync(
            f"⏳ Reddit rate limit exhausted for r/{subreddit}: {exc}",
            s.slack_alert_channel,
        )
        raise

    if not top_posts and not hot_posts:
        logger.warning("No posts returned for r/%s — skipping", subreddit)
        return

    skill = RedditReadSkill()
    ai_summary = await skill._ai_summary(subreddit, top_posts or hot_posts)
    viral = skill._detect_viral(hot_posts)
    digest = skill._format_digest(subreddit, top_posts, ai_summary, viral, time_filter)

    try:
        post_alert_sync(digest, channel)
    except Exception as exc:
        logger.error("Slack delivery failed for r/%s digest: %s", subreddit, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_due(cron_expr: str, window_start: datetime, now: datetime) -> bool:
    """Return True if the cron expression has a tick within (window_start, now]."""
    try:
        from croniter import croniter

        it = croniter(cron_expr, window_start)
        next_tick: datetime = it.get_next(datetime)
        # Make timezone-aware if naive
        if next_tick.tzinfo is None:
            next_tick = next_tick.replace(tzinfo=timezone.utc)
        return next_tick <= now
    except Exception as exc:
        logger.warning("Invalid cron expression '%s': %s", cron_expr, exc)
        return False


def _load_schedules() -> list[dict]:
    try:
        from app.memory.redis_client import RedisMemory

        r = RedisMemory().client
        raw = r.get(_REDIS_SCHEDULES_KEY)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to load Reddit schedules: %s", exc)
    return []
