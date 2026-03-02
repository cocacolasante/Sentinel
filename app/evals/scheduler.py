"""
Eval scheduler — APScheduler jobs wired into the FastAPI lifespan.

Schedule:
  Weekly  — Sunday 09:00 UTC: run all agent evals, post Slack scorecard
  Nightly — 02:00 UTC daily:  run integration reliability checks

Starts automatically when the Brain server starts.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ── Job functions ─────────────────────────────────────────────────────────────

async def _run_weekly_agent_evals() -> None:
    """Weekly job: run all agent evals and post Slack scorecard."""
    logger.info("Scheduled job: weekly agent evals starting")
    try:
        from app.evals.runner   import EvalRunner
        from app.evals.reporter import post_scorecard_to_slack

        runner    = EvalRunner()
        summaries = await runner.run_all_agents()

        # Collect previous scores for delta display
        previous: dict[str, float] = {}
        for summary in summaries:
            prev = runner.get_previous_avg(summary.agent_name, exclude_run_id=summary.run_id)
            if prev is not None:
                previous[summary.agent_name] = prev

        await post_scorecard_to_slack(summaries, previous_scores=previous)
        logger.info(
            "Weekly evals complete | %d agents | avg %.1f",
            len(summaries),
            sum(s.avg_score for s in summaries) / len(summaries) if summaries else 0,
        )
    except Exception as exc:
        logger.error("Weekly agent eval job failed: %s", exc)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass


async def _run_nightly_integration_evals() -> None:
    """Nightly job: check all integrations are reachable and working."""
    logger.info("Scheduled job: nightly integration evals starting")
    try:
        from app.evals.integrations import run_all_integration_evals
        results = await run_all_integration_evals()
        passed  = sum(1 for r in results if r.passed)
        logger.info("Nightly integration evals: %d/%d passed", passed, len(results))
    except Exception as exc:
        logger.error("Nightly integration eval job failed: %s", exc)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def start_scheduler() -> AsyncIOScheduler:
    """Create, configure, and start the APScheduler instance."""
    global _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Weekly agent evals — Sunday at 09:00 UTC
    scheduler.add_job(
        _run_weekly_agent_evals,
        CronTrigger(day_of_week="sun", hour=9, minute=0),
        id="weekly_agent_evals",
        name="Weekly Agent Quality Evals",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1hr late start
    )

    # Nightly integration evals — 02:00 UTC daily
    scheduler.add_job(
        _run_nightly_integration_evals,
        CronTrigger(hour=2, minute=0),
        id="nightly_integration_evals",
        name="Nightly Integration Reliability Evals",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()
    _scheduler = scheduler

    jobs = scheduler.get_jobs()
    for job in jobs:
        logger.info(
            "Scheduled: %s | next run: %s",
            job.name,
            job.next_run_time.strftime("%Y-%m-%d %H:%M UTC") if job.next_run_time else "unknown",
        )

    return scheduler


def stop_scheduler() -> None:
    """Gracefully stop the scheduler on app shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Eval scheduler stopped")
    _scheduler = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler
