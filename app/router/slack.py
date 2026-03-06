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

logger = logging.getLogger(__name__)
settings = get_settings()

# Suppress the SDK's noisy ERROR-level logs for transient session-monitor
# failures — the SDK handles reconnection internally, so these are not
# actionable and create false Sentry alerts.
class _SuppressSocketMonitorErrors(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Failed to check the cur" not in record.getMessage()

logging.getLogger("slack_sdk.socket_mode.aiohttp").addFilter(
    _SuppressSocketMonitorErrors()
)

dispatch = Dispatcher()

# Bot user ID — fetched once at startup; used to detect thread replies to bot messages
_bot_user_id: str = ""

# Phrases that trigger the built-in skills/help listing (no LLM call)
_HELP_PHRASES = {
    "help",
    "skills",
    "list skills",
    "what can you do",
    "capabilities",
    "what skills do you have",
    "show skills",
    "available skills",
    "what can you do?",
    "help me",
}

# Intent → friendly label for the response header
_INTENT_LABELS: dict[str, str] = {
    "gmail_read": "📧 Gmail read",
    "gmail_send": "📧 Gmail send",
    "gmail_reply": "📧 Gmail reply",
    "calendar_read": "📅 Calendar read",
    "calendar_write": "📅 Calendar event",
    "github_read": "🐙 GitHub read",
    "github_write": "🐙 GitHub write",
    "smart_home": "🏠 Smart home",
    "n8n_execute": "⚡ n8n workflow",
    "n8n_manage": "⚡ n8n manage",
    "cicd_read": "🔄 CI/CD read",
    "cicd_trigger": "🔄 CI/CD trigger",
    "contacts_read": "👤 Contacts read",
    "contacts_write": "👤 Contacts write",
    "whatsapp_read": "💬 WhatsApp read",
    "whatsapp_send": "💬 WhatsApp send",
    "ionos_cloud": "☁️ IONOS cloud",
    "ionos_dns": "🌐 IONOS DNS",
    "research": "🔍 Research",
    "code": "💻 Code",
    "content_draft": "✍️ Content draft",
    "social_caption": "📱 Social caption",
    "ad_copy": "📣 Ad copy",
    "content_repurpose": "♻️ Content repurpose",
    "content_calendar": "🗓️ Content calendar",
    "repo_read": "📂 Repo read",
    "repo_write": "✏️ Repo write",
    "repo_commit": "🚀 Repo commit",
    "sentry_read": "🐛 Sentry read",
    "sentry_manage": "🐛 Sentry manage",
    "server_shell": "🖥️ Server shell",
    "skill_discover": "🔎 Skill discovery",
    "deploy": "🚀 Deploy",
    "task_create": "📋 Task created",
    "task_read": "📋 Tasks",
    "task_update": "📋 Task updated",
    "chat": "💭 Chat",
    "slack_read": "💬 Slack read",
    "rate_limited": "⏱️ Rate limited",
    "blocked": "⛔ Blocked",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _session_id(event: dict) -> str:
    # Keyed by user only — so memory is shared across DMs and channel mentions.
    # This also feeds the cross-interface primary session so Slack context
    # appears in CLI and REST API responses.
    user = event.get("user", "unknown")
    return f"slack:{user}"


def _strip_mention(text: str) -> str:
    if text.startswith("<@"):
        text = text.split(">", 1)[-1].strip()
    return text


def _build_skills_help() -> str:
    """Formatted list of all registered skills with descriptions."""
    reg = dispatch.skills
    available_lines = []
    unavailable_lines = []

    for skill in reg._all:
        intents = " | ".join(f"`{i}`" for i in skill.trigger_intents) if skill.trigger_intents else "_(fallback)_"
        line = f"• {intents} — {skill.description}"
        if skill.is_available():
            available_lines.append(line)
        else:
            unavailable_lines.append(f"{line} _(not configured)_")

    parts = ["*🧠 Sentinel Skills — All Capabilities*\n"]
    if available_lines:
        parts.append("*Ready to use:*")
        parts.extend(available_lines)
    if unavailable_lines:
        parts.append("\n*Needs API keys / configuration:*")
        parts.extend(unavailable_lines)
    parts.append(
        "\n*✏️ Codebase self-editing workflow:*\n"
        "```\n"
        '1. Read a file:    "read app/brain/intent.py"\n'
        '2. Make a change:  "patch app/brain/intent.py — add task_create routing hint"\n'
        "3. Confirm:        reply  confirm  (or  cancel  to abort)\n"
        '4. Commit + push:  "commit these changes with message: add task routing"\n'
        '5. Deploy:         "deploy" — rebuilds the Docker image from the latest code\n'
        "```\n"
        'You can also say _"show git status"_, _"list files in app/skills"_, or '
        '_"what changed since the last commit"_.\n'
    )
    parts.append(
        "*Usage tips:*\n"
        '• Be specific: _"send an email to john@co.com about the meeting rescheduled to Friday"_\n'
        "• Confirm writes: reply `confirm` or `cancel` when prompted\n"
        '• Task board: _"create a task: fix the login bug, priority 4"_ / _"list my tasks"_\n'
        "• Type `help` anytime to see this list"
    )
    return "\n".join(parts)


def _format_reply(result: DispatchResult, session_id: str = "") -> str:
    """Format a DispatchResult as a clean Slack message with intent header + summary."""
    intent = result.intent
    agent = result.agent
    reply = result.reply.strip()

    label = _INTENT_LABELS.get(intent, f"`{intent}`")

    header_parts = [f"✅ {label}"]
    if agent and agent not in ("default", ""):
        header_parts.append(f"_via {agent} agent_")
    if session_id:
        header_parts.append(f"_`{session_id}`_")

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
    channel = event.get("channel")
    ack_ts: str | None = None

    # 1. Immediate ACK — visible within ~100 ms
    try:
        ack_resp = await say("✅ Received — 🧠 thinking...")
        ack_ts = ack_resp.get("ts")
    except Exception as exc:
        logger.warning("Could not send Slack ACK: %s", exc)

    # 1b. Store Slack context so background tasks can post back to this thread
    if channel and ack_ts:
        try:
            from app.memory.redis_client import RedisMemory

            RedisMemory().set_slack_context(session_id, channel, ack_ts)
        except Exception:
            pass

    # 2. Dispatch through the Brain
    try:
        result = await dispatch.process(text, session_id)
        reply = _format_reply(result, session_id=session_id)
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


# ── App factory ───────────────────────────────────────────────────────────────


def _build_app() -> AsyncApp:
    """Create AsyncApp and register event listeners.

    Called lazily inside start_socket_mode() so that importing this module
    never instantiates AsyncApp — avoiding BoltError when Slack credentials
    are absent (e.g. in CI / test environments).
    """
    app = AsyncApp(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )

    @app.event("message")
    async def handle_message(event, say, client):
        subtype = event.get("subtype")
        channel_type = event.get("channel_type")
        thread_ts = event.get("thread_ts")
        is_bot = bool(event.get("bot_id"))

        # Thread reply to a bot message in any channel
        if thread_ts and not is_bot and not subtype:
            parent_user = event.get("parent_user_id", "")
            if parent_user == _bot_user_id and _bot_user_id:
                await _handle_thread_reply(event, client)
                return

        # Direct message (existing behaviour)
        if channel_type == "im" and not subtype:
            await _handle(event, say, client)

    @app.event("app_mention")
    async def handle_mention(event, say, client):
        await _handle(event, say, client)

    return app


async def _handle_thread_reply(event: dict, client) -> None:
    """Process a user reply in a thread where the parent message was from this bot."""
    channel = event["channel"]
    thread_ts = event["thread_ts"]
    user_text = event.get("text", "").strip()
    if not user_text:
        return

    # Enrich with parent message context from Redis (stored when bot originally posted)
    try:
        from app.memory.redis_client import RedisMemory
        _redis = RedisMemory()
        parent_ctx = _redis.client.get(f"sentinel:msg:{channel}:{thread_ts}") or ""
        parent_text = parent_ctx if isinstance(parent_ctx, str) else parent_ctx.decode()
    except Exception:
        parent_text = ""

    augmented = user_text
    if parent_text:
        augmented = f"{user_text}\n\n[Context — Sentinel's original message: {parent_text[:400]}]"

    session_id = _session_id(event)

    # Store Slack context so background tasks post back to this thread
    try:
        from app.memory.redis_client import RedisMemory
        RedisMemory().set_slack_context(session_id, channel, thread_ts)
    except Exception:
        pass

    try:
        ack_resp = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="✅ Received — 🧠 thinking..."
        )
        ack_ts = ack_resp.get("ts")
    except Exception as exc:
        logger.warning("Could not send thread reply ACK: %s", exc)
        return

    try:
        result = await dispatch.process(augmented, session_id)
        reply = _format_reply(result, session_id=session_id)
    except Exception as exc:
        logger.error("Thread reply dispatch error: %s", exc, exc_info=True)
        reply = f"❌ *Error* — `{type(exc).__name__}: {exc}`"

    try:
        await client.chat_update(channel=channel, ts=ack_ts, text=reply)
    except Exception as exc:
        logger.warning("chat_update failed for thread reply: %s", exc)
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)


# ── Socket Mode launcher ──────────────────────────────────────────────────────


async def _auto_join_channels(client) -> None:
    """
    Join all configured Sentinel channels at startup.

    Requires the bot token to have `channels:read` + `channels:join` scopes.
    If those scopes are missing this logs a clear warning and exits gracefully —
    the user must add the scopes in the Slack app dashboard and reinstall.
    """
    target_channels = {
        c for c in [
            settings.slack_alert_channel,
            settings.slack_eval_channel,
            getattr(settings, "slack_milestone_channel", ""),
            getattr(settings, "slack_tasks_channel", ""),
            getattr(settings, "slack_rmm_prod_channel", ""),
            getattr(settings, "slack_rmm_dev_channel", ""),
        ] if c
    }

    # Step 1: resolve channel names → IDs (needs channels:read)
    name_to_id: dict[str, str] = {}
    try:
        cursor = None
        while True:
            kwargs = {"types": "public_channel", "exclude_archived": True, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await client.conversations_list(**kwargs)
            if not resp.get("ok"):
                err = resp.get("error", "unknown")
                if err == "missing_scope":
                    logger.warning(
                        "Slack auto-join skipped — bot is missing 'channels:read' scope. "
                        "Add it at api.slack.com → Your App → OAuth & Permissions → Bot Token Scopes, "
                        "then click 'Reinstall to Workspace' and restart the bot."
                    )
                else:
                    logger.warning("conversations_list failed: %s", err)
                return
            for ch in resp.get("channels", []):
                name_to_id[ch["name"]] = ch["id"]
                if ch.get("is_member"):
                    logger.debug("Already a member of #%s", ch["name"])
            meta = resp.get("response_metadata", {})
            cursor = meta.get("next_cursor", "")
            if not cursor:
                break
    except Exception as exc:
        logger.warning("Could not list Slack channels: %s", exc)
        return

    # Step 2: join any target channel we're not in yet (needs channels:join)
    for name in sorted(target_channels):
        ch_id = name_to_id.get(name)
        if not ch_id:
            logger.warning("Slack channel #%s not found in workspace — create it first", name)
            continue
        try:
            resp = await client.conversations_join(channel=ch_id)
            if resp.get("ok"):
                logger.info("Joined Slack channel #%s (%s)", name, ch_id)
            else:
                err = resp.get("error", "unknown")
                if err == "already_in_channel":
                    logger.debug("Already in #%s", name)
                elif err == "missing_scope":
                    logger.warning(
                        "Slack auto-join skipped — bot is missing 'channels:join' scope. "
                        "Add it at api.slack.com → Your App → OAuth & Permissions → Bot Token Scopes, "
                        "then click 'Reinstall to Workspace' and restart the bot."
                    )
                    return
                else:
                    logger.warning("Could not join #%s: %s", name, err)
        except Exception as exc:
            logger.warning("conversations_join failed for #%s: %s", name, exc)


async def start_socket_mode() -> None:
    global _bot_user_id

    if not settings.slack_app_token or not settings.slack_bot_token:
        logger.warning(
            "Slack tokens not configured — bot will not start. "
            "Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env"
        )
        return

    slack_app = _build_app()

    from slack_sdk.web.async_client import AsyncWebClient
    _wc = AsyncWebClient(token=settings.slack_bot_token)

    # Fetch bot user ID once so thread-reply handler can identify bot messages
    try:
        resp = await _wc.auth_test()
        _bot_user_id = resp.get("user_id", "")
        logger.info("Slack bot user ID: %s", _bot_user_id)
    except Exception as exc:
        logger.warning("Could not fetch bot user ID: %s", exc)

    # Auto-join configured channels (no-op if scopes not yet granted)
    await _auto_join_channels(_wc)

    backoff = 5  # seconds; doubles on each consecutive failure, caps at 300

    while True:
        handler = AsyncSocketModeHandler(slack_app, settings.slack_app_token)
        logger.info("Slack Socket Mode connecting...")
        try:
            await handler.start_async()
            # start_async() only returns if the connection is cleanly closed
            logger.warning("Slack Socket Mode connection closed — reconnecting in %ds", backoff)
        except asyncio.CancelledError:
            logger.info("Slack Socket Mode cancelled — shutting down")
            return
        except Exception as exc:
            exc_name = type(exc).__name__
            logger.error(
                "Slack Socket Mode exited unexpectedly (%s): %s — reconnecting in %ds",
                exc_name, exc, backoff,
            )

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)
