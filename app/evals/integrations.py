"""
Integration reliability evals — nightly read-only checks.

Each check tests that an integration can successfully complete a real API call.
All checks are read-only — no emails sent, no events created, no state changed.

Results are stored in Postgres for uptime tracking (7-day rolling window).
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.evals.base import IntegrationEvalResult

logger = logging.getLogger(__name__)


# ── Individual integration checks ────────────────────────────────────────────

async def _check_gmail() -> IntegrationEvalResult:
    t0 = time.monotonic()
    try:
        from app.integrations.gmail import GmailClient
        client = GmailClient()
        if not client.is_configured():
            return IntegrationEvalResult("gmail", False, None, "Not configured — GOOGLE_REFRESH_TOKEN missing")
        emails = await client.list_emails(query="is:unread", max_results=1)
        if not isinstance(emails, list):
            raise ValueError(f"Expected list, got {type(emails)}")
        return IntegrationEvalResult("gmail", True, round((time.monotonic() - t0) * 1000, 1), None)
    except Exception as exc:
        return IntegrationEvalResult("gmail", False, round((time.monotonic() - t0) * 1000, 1), str(exc))


async def _check_calendar() -> IntegrationEvalResult:
    t0 = time.monotonic()
    try:
        from app.integrations.google_calendar import CalendarClient
        client = CalendarClient()
        if not client.is_configured():
            return IntegrationEvalResult("calendar", False, None, "Not configured — GOOGLE_REFRESH_TOKEN missing")
        events = await client.list_events(period="today")
        if not isinstance(events, list):
            raise ValueError(f"Expected list, got {type(events)}")
        return IntegrationEvalResult("calendar", True, round((time.monotonic() - t0) * 1000, 1), None)
    except Exception as exc:
        return IntegrationEvalResult("calendar", False, round((time.monotonic() - t0) * 1000, 1), str(exc))


async def _check_github() -> IntegrationEvalResult:
    t0 = time.monotonic()
    try:
        from app.integrations.github import GitHubClient
        client = GitHubClient()
        if not client.is_configured():
            return IntegrationEvalResult("github", False, None, "Not configured — GITHUB_TOKEN missing")
        notifications = await client.list_notifications()
        if not isinstance(notifications, list):
            raise ValueError(f"Expected list, got {type(notifications)}")
        return IntegrationEvalResult("github", True, round((time.monotonic() - t0) * 1000, 1), None)
    except Exception as exc:
        return IntegrationEvalResult("github", False, round((time.monotonic() - t0) * 1000, 1), str(exc))


async def _check_n8n() -> IntegrationEvalResult:
    t0 = time.monotonic()
    try:
        from app.integrations.n8n_bridge import N8nBridge
        bridge = N8nBridge()
        if not bridge.is_configured():
            return IntegrationEvalResult("n8n", False, None, "Not configured — N8N_WEBHOOK_URL missing")
        reachable = await bridge.health()
        if not reachable:
            raise ConnectionError("n8n health check returned False")
        return IntegrationEvalResult("n8n", True, round((time.monotonic() - t0) * 1000, 1), None)
    except Exception as exc:
        return IntegrationEvalResult("n8n", False, round((time.monotonic() - t0) * 1000, 1), str(exc))


async def _check_home_assistant() -> IntegrationEvalResult:
    t0 = time.monotonic()
    try:
        from app.integrations.home_assistant import HomeAssistantClient
        client = HomeAssistantClient()
        if not client.is_configured():
            return IntegrationEvalResult("home_assistant", False, None, "Not configured — HOME_ASSISTANT_URL missing")
        reachable = await client.health()
        if not reachable:
            raise ConnectionError("Home Assistant health check returned False")
        return IntegrationEvalResult("home_assistant", True, round((time.monotonic() - t0) * 1000, 1), None)
    except Exception as exc:
        return IntegrationEvalResult("home_assistant", False, round((time.monotonic() - t0) * 1000, 1), str(exc))


# ── Runner ────────────────────────────────────────────────────────────────────

_CHECKS = [_check_gmail, _check_calendar, _check_github, _check_n8n, _check_home_assistant]


async def run_all_integration_evals() -> list[IntegrationEvalResult]:
    """Run all integration checks concurrently. Returns results for all integrations."""
    results = await asyncio.gather(*[check() for check in _CHECKS], return_exceptions=False)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        logger.info("Integration eval | %s | %s | %s", r.integration, status, r.error or f"{r.latency_ms}ms")

    await asyncio.to_thread(_persist_integration_results, list(results))
    return list(results)


def _persist_integration_results(results: list[IntegrationEvalResult]) -> None:
    try:
        from app.db import postgres
        for r in results:
            postgres.execute(
                """
                INSERT INTO integration_eval_results
                    (integration, passed, latency_ms, error_message)
                VALUES (%s, %s, %s, %s)
                """,
                (r.integration, r.passed, r.latency_ms, r.error),
            )
    except Exception as exc:
        logger.error("Failed to persist integration eval results: %s", exc)


def get_uptime_pct(integration: str, days: int = 7) -> float | None:
    """Return rolling uptime percentage for an integration over the last N days."""
    try:
        from app.db import postgres
        row = postgres.execute_one(
            """
            SELECT
                COUNT(*) FILTER (WHERE passed) AS passed_count,
                COUNT(*) AS total_count
            FROM integration_eval_results
            WHERE integration = %s
              AND checked_at > NOW() - INTERVAL '%s days'
            """,
            (integration, days),
        )
        if not row or not row["total_count"]:
            return None
        return round(row["passed_count"] / row["total_count"] * 100, 1)
    except Exception:
        return None
