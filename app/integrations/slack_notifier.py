"""
Slack Notifier — shared alert posting utility.

Used by:
  - SentryWebhook  (async, critical error alerts)
  - EvalTasks      (async/sync, integration health, scorecard)
  - HealthCheck    (async, system health alerts)
  - CostTracker    (sync, budget threshold alerts — already in cost_tracker.py)
"""

from __future__ import annotations

import logging

from app.config import get_settings

logger = logging.getLogger(__name__)


async def post_alert(text: str, channel: str | None = None) -> bool:
    """Post a message to Slack asynchronously. Returns True on success."""
    settings = get_settings()
    if not settings.slack_bot_token:
        logger.warning("Slack bot token not configured — skipping notification")
        return False

    target = channel or settings.slack_alert_channel or "brain-alerts"
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        client = AsyncWebClient(token=settings.slack_bot_token)
        resp   = await client.chat_postMessage(channel=target, text=text, mrkdwn=True)
        if resp.get("ok"):
            logger.info("Slack alert posted | channel=%s", target)
            return True
        logger.error("Slack alert failed: %s", resp.get("error"))
        return False
    except Exception as exc:
        logger.error("Slack alert exception: %s", exc)
        return False


def post_thread_reply_sync(text: str, channel: str, thread_ts: str) -> bool:
    """Post a threaded reply synchronously — used by background Celery tasks to report back."""
    settings = get_settings()
    if not settings.slack_bot_token:
        return False
    try:
        from slack_sdk import WebClient
        resp = WebClient(token=settings.slack_bot_token).chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            mrkdwn=True,
        )
        return bool(resp.get("ok"))
    except Exception as exc:
        logger.error("Slack thread reply (sync) exception: %s", exc)
        return False


async def post_dm(text: str, user_id: str | None = None) -> bool:
    """Open a DM with the owner user and post a message asynchronously."""
    settings = get_settings()
    target = user_id or settings.slack_owner_user_id
    if not settings.slack_bot_token or not target:
        logger.warning("Slack DM skipped — bot token or owner user ID not configured")
        return False
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        client = AsyncWebClient(token=settings.slack_bot_token)
        conv   = await client.conversations_open(users=target)
        dm_channel = conv["channel"]["id"]
        resp = await client.chat_postMessage(channel=dm_channel, text=text, mrkdwn=True)
        if resp.get("ok"):
            logger.info("Slack DM posted | user=%s", target)
            return True
        logger.error("Slack DM failed: %s", resp.get("error"))
        return False
    except Exception as exc:
        logger.error("Slack DM exception: %s", exc)
        return False


def post_dm_sync(text: str, user_id: str | None = None) -> bool:
    """Open a DM with the owner user and post a message synchronously (safe in Celery)."""
    settings = get_settings()
    target = user_id or settings.slack_owner_user_id
    if not settings.slack_bot_token or not target:
        return False
    try:
        from slack_sdk import WebClient
        client = WebClient(token=settings.slack_bot_token)
        conv   = client.conversations_open(users=target)
        dm_channel = conv["channel"]["id"]
        resp = client.chat_postMessage(channel=dm_channel, text=text, mrkdwn=True)
        return bool(resp.get("ok"))
    except Exception as exc:
        logger.error("Slack DM (sync) exception: %s", exc)
        return False


def post_alert_sync(text: str, channel: str | None = None) -> bool:
    """Post a message to Slack synchronously (safe inside Celery tasks)."""
    settings = get_settings()
    if not settings.slack_bot_token:
        return False

    target = channel or settings.slack_alert_channel or "brain-alerts"
    try:
        from slack_sdk import WebClient
        resp = WebClient(token=settings.slack_bot_token).chat_postMessage(
            channel=target, text=text, mrkdwn=True,
        )
        return bool(resp.get("ok"))
    except Exception as exc:
        logger.error("Slack alert (sync) exception: %s", exc)
        return False
