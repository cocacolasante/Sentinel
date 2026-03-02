"""
Slack Bot Handler — Socket Mode

Uses the Dispatcher so Slack messages get the same intent routing,
integration calls, and LLM augmentation as REST /chat requests.

Session ID format: slack:{user_id}:{channel_id}
This gives each user a separate memory context per channel.
"""

import asyncio
import logging

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from app.brain.dispatcher import Dispatcher
from app.config           import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)

dispatch = Dispatcher()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _session_id(event: dict) -> str:
    user    = event.get("user",    "unknown")
    channel = event.get("channel", "dm")
    return f"slack:{user}:{channel}"


def _strip_mention(text: str) -> str:
    if text.startswith("<@"):
        text = text.split(">", 1)[-1].strip()
    return text


async def _handle(event: dict, say) -> None:
    text = _strip_mention(event.get("text", "").strip())
    if not text:
        return

    session_id = _session_id(event)

    try:
        result = await dispatch.process(text, session_id)
        await say(result.reply)
    except Exception as exc:
        logger.error("Slack handler error: %s", exc)
        await say("Something went wrong — please try again.")


# ── Event listeners ───────────────────────────────────────────────────────────

@slack_app.event("message")
async def handle_dm(event, say):
    if event.get("channel_type") == "im" and not event.get("subtype"):
        await _handle(event, say)


@slack_app.event("app_mention")
async def handle_mention(event, say):
    await _handle(event, say)


# ── Socket Mode launcher ──────────────────────────────────────────────────────

async def start_socket_mode() -> None:
    if not settings.slack_app_token or not settings.slack_bot_token:
        logger.warning(
            "Slack tokens not configured — bot will not start. "
            "Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env"
        )
        return
    handler = AsyncSocketModeHandler(slack_app, settings.slack_app_token)
    logger.info("Slack Socket Mode connecting...")
    await handler.start_async()
