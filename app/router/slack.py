"""
Slack Bot Handler — Socket Mode

Behaviour for every incoming message:
  1. Immediately posts "✅ Received — 🧠 thinking..." (visible ACK)
  2. Dispatches through the Brain (intent → skill → LLM)
  3. Updates that same message in-place with a formatted summary of what was done

Session ID format: slack:{user_id}:{channel_id}

Special keywords (case-insensitive):
  help | skills | list skills | what can you do  →  list all registered skills
"""

import asyncio
import logging

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from app.brain.dispatcher import Dispatcher, DispatchResult
from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)

dispatch = Dispatcher()

# Phrases that trigger the built-in skills/help listing (no LLM call)
_HELP_PHRASES = {
    "help", "skills", "list skills", "what can you do",
    "capabilities", "what skills do you have", "show skills",
    "available skills", "what can you do?", "help me",
}

# Intent → friendly label for the response header
_INTENT_LABELS: dict[str, str] = {
    "gmail_read":       "📧 Gmail read",
    "gmail_send":       "📧 Gmail send",
    "gmail_reply":      "📧 Gmail reply",
    "calendar_read":    "📅 Calendar read",
    "calendar_write":   "📅 Calendar event",
    "github_read":      "🐙 GitHub read",
    "github_write":     "🐙 GitHub write",
    "smart_home":       "🏠 Smart home",
    "n8n_execute":      "⚡ n8n workflow",
    "n8n_manage":       "⚡ n8n manage",
    "cicd_read":        "🔄 CI/CD read",
    "cicd_trigger":     "🔄 CI/CD trigger",
    "contacts_read":    "👤 Contacts read",
    "contacts_write":   "👤 Contacts write",
    "whatsapp_read":    "💬 WhatsApp read",
    "whatsapp_send":    "💬 WhatsApp send",
    "ionos_cloud":      "☁️ IONOS cloud",
    "ionos_dns":        "🌐 IONOS DNS",
    "research":         "🔍 Research",
    "code":             "💻 Code",
    "content_draft":    "✍️ Content draft",
    "social_caption":   "📱 Social caption",
    "ad_copy":          "📣 Ad copy",
    "content_repurpose":"♻️ Content repurpose",
    "content_calendar": "🗓️ Content calendar",
    "repo_read":        "📂 Repo read",
    "repo_write":       "✏️ Repo write",
    "repo_commit":      "🚀 Repo commit",
    "sentry_read":      "🐛 Sentry read",
    "sentry_manage":    "🐛 Sentry manage",
    "server_shell":     "🖥️ Server shell",
    "skill_discover":   "🔎 Skill discovery",
    "chat":             "💭 Chat",
    "rate_limited":     "⏱️ Rate limited",
    "blocked":          "⛔ Blocked",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _session_id(event: dict) -> str:
    user    = event.get("user",    "unknown")
    channel = event.get("channel", "dm")
    return f"slack:{user}:{channel}"


def _strip_mention(text: str) -> str:
    if text.startswith("<@"):
        text = text.split(">", 1)[-1].strip()
    return text


def _build_skills_help() -> str:
    """Formatted list of all registered skills with descriptions."""
    reg = dispatch.skills
    available_lines   = []
    unavailable_lines = []

    for skill in reg._all:
        intents = " | ".join(f"`{i}`" for i in skill.trigger_intents) if skill.trigger_intents else "_(fallback)_"
        line    = f"• {intents} — {skill.description}"
        if skill.is_available():
            available_lines.append(line)
        else:
            unavailable_lines.append(f"{line} _(not configured)_")

    parts = ["*🧠 Brain Skills — All Capabilities*\n"]
    if available_lines:
        parts.append("*Ready to use:*")
        parts.extend(available_lines)
    if unavailable_lines:
        parts.append("\n*Needs API keys / configuration:*")
        parts.extend(unavailable_lines)
    parts.append(
        "\n*Usage tips:*\n"
        "• Be specific: _\"send an email to john@co.com about the meeting rescheduled to Friday\"_\n"
        "• Code changes: _\"read app/brain/dispatcher.py\"_ → _\"patch the file to improve X\"_\n"
        "• Confirm writes: reply `confirm` or `cancel` when prompted\n"
        "• Type `help` anytime to see this list"
    )
    return "\n".join(parts)


def _format_reply(result: DispatchResult) -> str:
    """Format a DispatchResult as a clean Slack message with intent header + summary."""
    intent = result.intent
    agent  = result.agent
    reply  = result.reply.strip()

    label = _INTENT_LABELS.get(intent, f"`{intent}`")

    header_parts = [f"✅ {label}"]
    if agent and agent not in ("default", ""):
        header_parts.append(f"_via {agent} agent_")

    header = "  ·  ".join(header_parts)
    divider = "─" * 36

    return f"{header}\n{divider}\n{reply}"


# ── Core handler ──────────────────────────────────────────────────────────────

async def _handle(event: dict, say, client) -> None:
    text = _strip_mention(event.get("text", "").strip())
    if not text:
        return

    # Built-in help / skills listing
    if text.lower() in _HELP_PHRASES:
        await say(_build_skills_help())
        return

    session_id = _session_id(event)
    channel    = event.get("channel")
    ack_ts: str | None = None

    # 1. Immediate ACK — visible within ~100 ms
    try:
        ack_resp = await say("✅ Received — 🧠 thinking...")
        ack_ts   = ack_resp.get("ts")
    except Exception as exc:
        logger.warning("Could not send Slack ACK: %s", exc)

    # 2. Dispatch through the Brain
    try:
        result = await dispatch.process(text, session_id)
        reply  = _format_reply(result)
    except Exception as exc:
        logger.error("Slack dispatch error: %s", exc, exc_info=True)
        reply = f"❌ *Error* — `{type(exc).__name__}: {exc}`"

    # 3. Update ACK message with the actual result
    if ack_ts and channel:
        try:
            await client.chat_update(channel=channel, ts=ack_ts, text=reply)
            return
        except Exception as exc:
            logger.warning("chat_update failed, falling back to new message: %s", exc)

    # Fallback: new message if update failed or no ACK ts
    await say(reply)


# ── Event listeners ───────────────────────────────────────────────────────────

@slack_app.event("message")
async def handle_dm(event, say, client):
    if event.get("channel_type") == "im" and not event.get("subtype"):
        await _handle(event, say, client)


@slack_app.event("app_mention")
async def handle_mention(event, say, client):
    await _handle(event, say, client)


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
    try:
        await handler.start_async()
    except Exception as exc:
        # The SDK reconnects automatically on transient WebSocket errors.
        # Log unexpected failures without propagating them — propagating would
        # kill the Brain's lifespan task and take down the whole server.
        exc_name = type(exc).__name__
        if exc_name in ("ClientConnectionResetError", "ConnectionResetError",
                        "ServerConnectionError", "CancelledError"):
            logger.debug("Slack socket closed ({}): SDK will reconnect", exc_name)
        else:
            logger.error("Slack Socket Mode exited unexpectedly ({}): {}", exc_name, exc)
