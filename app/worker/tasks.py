"""
Celery tasks — the concrete work that beat schedules and workers execute.

Tasks use asyncio.run() because Celery workers are synchronous processes.
Each task has a retry policy and time limit appropriate to its workload.
"""

from __future__ import annotations

import asyncio
import logging

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


# ── Scheduled tasks ───────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.worker.tasks.run_weekly_agent_evals",
    queue="evals",
    max_retries=2,
    default_retry_delay=300,   # 5 min between retries
    soft_time_limit=3_600,     # 1hr soft limit — logs warning
    time_limit=3_900,          # 1hr 5min hard limit — kills worker
)
def run_weekly_agent_evals(self) -> dict:
    """Run all agent quality evals and post Slack scorecard."""
    try:
        return asyncio.run(_weekly_evals())
    except Exception as exc:
        logger.error("Weekly agent eval task failed: %s", exc)
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="app.worker.tasks.run_nightly_integration_evals",
    queue="evals",
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,
    time_limit=660,
)
def run_nightly_integration_evals(self) -> dict:
    """Read-only checks of all configured integrations."""
    try:
        return asyncio.run(_nightly_evals())
    except Exception as exc:
        logger.error("Nightly integration eval task failed: %s", exc)
        raise self.retry(exc=exc)


# ── Async implementations ──────────────────────────────────────────────────────

async def _weekly_evals() -> dict:
    from app.evals.runner   import EvalRunner
    from app.evals.reporter import post_scorecard_to_slack

    runner    = EvalRunner()
    summaries = await runner.run_all_agents()

    previous: dict[str, float] = {}
    for s in summaries:
        prev = runner.get_previous_avg(s.agent_name, exclude_run_id=s.run_id)
        if prev is not None:
            previous[s.agent_name] = prev

    await post_scorecard_to_slack(summaries, previous_scores=previous)
    avg = sum(s.avg_score for s in summaries) / len(summaries) if summaries else 0.0
    logger.info("Weekly evals complete | %d agents | avg %.1f", len(summaries), avg)
    return {"agents": len(summaries), "avg_score": round(avg, 2)}


async def _nightly_evals() -> dict:
    from app.evals.integrations import run_all_integration_evals

    results = await run_all_integration_evals()
    passed  = sum(1 for r in results if r.passed)
    logger.info("Nightly integration evals | %d/%d passed", passed, len(results))
    return {"total": len(results), "passed": passed}
