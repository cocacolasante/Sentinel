"""
EvalReporter — formats and posts the weekly scorecard to Slack.

Scorecard format:
  🧠 Weekly Brain Eval Report — 2026-03-01
  Engineer:    8.2/10 ✅  (+0.3 vs last week)  [3/3 passed]
  Writer:      7.4/10 ✅  (baseline)            [3/3 passed]
  Researcher:  6.8/10 ⚠️  (-0.6 vs last week)  [2/3 passed]
  Strategist:  7.1/10 ✅  (+0.1 vs last week)  [3/3 passed]
  Marketing:   5.9/10 ❌  (-1.2 — degraded)    [1/3 passed]

  Integration uptime (7d):
  Gmail: 100%  ·  Calendar: 98%  ·  GitHub: 95%  ·  n8n: 100%  ·  HA: 87%

  Run ID: abc12345 · 2026-03-01 09:00 UTC
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.evals.base import AgentEvalSummary, IntegrationEvalResult
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_INTEGRATIONS_ORDER = ["gmail", "calendar", "github", "n8n", "home_assistant"]
_INTEGRATION_LABELS = {
    "gmail": "Gmail",
    "calendar": "Calendar",
    "github": "GitHub",
    "n8n": "n8n",
    "home_assistant": "HA",
}


def format_scorecard(
    summaries: list[AgentEvalSummary],
    integration_results: list[IntegrationEvalResult] | None = None,
    previous_scores: dict[str, float] | None = None,
) -> str:
    """Build the Slack message text for the weekly scorecard."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_id = summaries[0].run_id if summaries else "unknown"

    lines = [f"*🧠 Weekly Brain Eval Report — {now}*\n"]

    # ── Agent scores ─────────────────────────────────────────────────────────
    for summary in sorted(summaries, key=lambda s: s.agent_name):
        emoji = summary.status_emoji
        score_str = f"{summary.avg_score}/10"

        # Delta vs last week
        prev = (previous_scores or {}).get(summary.agent_name)
        if prev is not None:
            delta = summary.avg_score - prev
            sign = "+" if delta >= 0 else ""
            delta_str = f"  ({sign}{delta:.1f} vs last week)"
            if delta < -0.5:
                delta_str += " — *degraded*"
        else:
            delta_str = "  (baseline)"

        pass_str = f"[{summary.passed_tests}/{summary.total_tests} passed]"

        agent_label = summary.agent_name.title().ljust(12)
        lines.append(f"{emoji}  *{agent_label}*  {score_str}{delta_str}  {pass_str}")

    # ── Integration uptime ────────────────────────────────────────────────────
    if integration_results:
        lines.append("\n*Integration uptime (7d rolling):*")
        uptime_parts: list[str] = []
        from app.evals.integrations import get_uptime_pct

        result_map = {r.integration: r for r in integration_results}
        for key in _INTEGRATIONS_ORDER:
            label = _INTEGRATION_LABELS.get(key, key)
            uptime = get_uptime_pct(key, days=7)
            if uptime is None:
                # Fall back to today's single result
                r = result_map.get(key)
                if r:
                    uptime_parts.append(f"{label}: {'✅' if r.passed else '❌'}")
            else:
                icon = "✅" if uptime >= 95 else ("⚠️" if uptime >= 80 else "❌")
                uptime_parts.append(f"{label}: {icon} {uptime}%")

        lines.append("  " + "  ·  ".join(uptime_parts))

    # ── Failed test details ───────────────────────────────────────────────────
    failed_details: list[str] = []
    for summary in summaries:
        for result in summary.results:
            if not result.passed and not result.error:
                failed_details.append(
                    f"  • *{summary.agent_name}/{result.test_name}* ({result.score}/10): _{result.reasoning[:120]}_"
                )

    if failed_details:
        lines.append("\n*Failed tests:*")
        lines.extend(failed_details[:5])  # cap at 5 to keep message readable
        if len(failed_details) > 5:
            lines.append(f"  _...and {len(failed_details) - 5} more_")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append(f"\n_Run ID: {run_id}  ·  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC_")

    return "\n".join(lines)


async def post_integration_health_to_slack(
    results: list[IntegrationEvalResult],
    channel: str | None = None,
) -> bool:
    """
    Post nightly integration health summary to Slack alerts channel.
    Only posts if there are any failures — no noise on all-green nights.
    """
    if not results:
        return True

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    # All passed — post a quiet confirmation to eval channel, not alerts
    if not failed:
        text = (
            f"✅ *Nightly Integration Health — All Green*\n"
            f"  {len(passed)}/{len(results)} integrations healthy\n"
            f"  _Checked at {datetime.now(timezone.utc).strftime('%H:%M UTC')}_"
        )
        target = channel or getattr(get_settings(), "slack_eval_channel", "brain-evals")
    else:
        # Failures → alert channel
        fail_list = "\n".join(f"  • *{r.integration}* — {r.error or 'check failed'}" for r in failed)
        text = (
            f"⚠️ *Nightly Integration Health — {len(failed)} Failing*\n"
            f"  {len(passed)}/{len(results)} healthy\n\n"
            f"*Failing integrations:*\n{fail_list}\n\n"
            f"_Check `GET /api/v1/integrations/status` for details_"
        )
        target = channel or getattr(get_settings(), "slack_alert_channel", "brain-alerts")

    try:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=get_settings().slack_bot_token)
        resp = await client.chat_postMessage(channel=target, text=text, mrkdwn=True)
        return bool(resp.get("ok"))
    except Exception as exc:
        logger.error("Failed to post integration health to Slack: %s", exc)
        return False


def get_settings():
    from app.config import get_settings as _gs

    return _gs()


async def post_scorecard_to_slack(
    summaries: list[AgentEvalSummary],
    integration_results: list[IntegrationEvalResult] | None = None,
    previous_scores: dict[str, float] | None = None,
    channel: str | None = None,
) -> bool:
    """
    Post the formatted scorecard to Slack.

    Args:
        channel: Slack channel ID or name (e.g. '#brain-evals').
                 Falls back to SLACK_EVAL_CHANNEL env var, then SLACK_DEFAULT_CHANNEL.
    Returns True on success.
    """
    if not settings.slack_bot_token:
        logger.warning("Slack bot token not configured — skipping scorecard post")
        return False

    target_channel = channel or getattr(settings, "slack_eval_channel", "") or "brain-evals"
    text = format_scorecard(summaries, integration_results, previous_scores)

    try:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=settings.slack_bot_token)
        response = await client.chat_postMessage(
            channel=target_channel,
            text=text,
            mrkdwn=True,
        )
        if response["ok"]:
            logger.info("Eval scorecard posted to Slack channel: %s", target_channel)
            return True
        logger.error("Slack post failed: %s", response.get("error"))
        return False
    except Exception as exc:
        logger.error("Failed to post eval scorecard to Slack: %s", exc)
        return False
