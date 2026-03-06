"""
Slack Read Skill — read channel history, DMs, and search messages

Actions:
  history        — fetch recent messages from a named channel
  search         — search for messages across all channels
  list_channels  — list all channels the bot is a member of
  dm_history     — fetch recent DM messages with a user
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)


def _fmt_ts(ts: str | None) -> str:
    """Convert Slack epoch timestamp to readable string."""
    if not ts:
        return "?"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def _clean_text(text: str) -> str:
    """Strip Slack user/channel mention tokens to readable text."""
    import re
    text = re.sub(r"<@[A-Z0-9]+>", "@user", text)
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text.strip()


class SlackReadSkill(BaseSkill):
    name = "slack_read"
    description = (
        "Read Slack channel history, DMs, or search messages. "
        "Can fetch recent messages from sentinel-alerts, sentinel-evals, "
        "sentinel-milestones, rmm-production, rmm-dev-staging, or any channel."
    )
    trigger_intents = ["slack_read"]
    approval_category = ApprovalCategory.NONE
    config_vars = ["SLACK_BOT_TOKEN"]

    def is_available(self) -> bool:
        from app.config import get_settings
        return bool(get_settings().slack_bot_token)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "history")

        if action == "list_channels":
            return await self._list_channels()
        if action == "search":
            return await self._search(params)
        if action == "history":
            return await self._history(params)
        if action == "dm_history":
            return await self._dm_history(params)

        return SkillResult(
            context_data="Unknown action. Use: history, search, list_channels, dm_history"
        )

    async def _get_client(self):
        from app.config import get_settings
        from slack_sdk.web.async_client import AsyncWebClient
        return AsyncWebClient(token=get_settings().slack_bot_token)

    async def _resolve_channel_id(self, client, name: str) -> str | None:
        """Resolve a channel name like 'sentinel-alerts' to its Slack channel ID."""
        name = name.lstrip("#")
        try:
            resp = await client.conversations_list(
                types="public_channel,private_channel", limit=200
            )
            if not resp.get("ok"):
                err = resp.get("error", "unknown")
                if err == "missing_scope":
                    logger.warning(
                        "channels:read scope missing — bot cannot list channels. "
                        "Add it at api.slack.com → Your App → OAuth & Permissions."
                    )
                return None
            for ch in resp.get("channels", []):
                if ch.get("name") == name or ch.get("id") == name:
                    return ch["id"]
        except Exception as exc:
            logger.warning("conversations_list failed: %s", exc)
        return None

    async def _list_channels(self) -> SkillResult:
        client = await self._get_client()
        try:
            resp = await client.conversations_list(
                types="public_channel,private_channel", limit=200
            )
            channels = resp.get("channels", [])
        except Exception as exc:
            return SkillResult(context_data=f"Failed to list channels: {exc}")

        if not channels:
            return SkillResult(context_data="No channels found (bot may not be in any channels).")

        lines = [f"**Slack Channels** ({len(channels)} visible)\n"]
        for ch in sorted(channels, key=lambda c: c.get("name", "")):
            members = ch.get("num_members", "?")
            is_private = "🔒" if ch.get("is_private") else "#"
            lines.append(f"{is_private} **{ch['name']}** — {members} members")
        return SkillResult(context_data="\n".join(lines))

    async def _history(self, params: dict) -> SkillResult:
        channel_name = (
            params.get("channel")
            or params.get("channel_name")
            or "sentinel-alerts"
        )
        limit = min(int(params.get("limit", 20)), 100)
        oldest = params.get("oldest", "")  # epoch string

        client = await self._get_client()
        channel_id = await self._resolve_channel_id(client, channel_name)

        if not channel_id:
            return SkillResult(
                context_data=(
                    f"Cannot read `#{channel_name}`. Either:\n"
                    f"1. The bot is missing `channels:read` scope — add it at "
                    f"api.slack.com → Your App → OAuth & Permissions → Bot Token Scopes, "
                    f"then click 'Reinstall to Workspace' and restart the bot.\n"
                    f"2. The channel doesn't exist — create `#{channel_name}` in Slack first.\n"
                    f"3. The bot hasn't been invited — once scopes are added the bot auto-joins "
                    f"on restart, or run `/invite @Sentinel` in `#{channel_name}`."
                )
            )

        try:
            kwargs: dict = {"channel": channel_id, "limit": limit}
            if oldest:
                kwargs["oldest"] = oldest
            resp = await client.conversations_history(**kwargs)
            messages = resp.get("messages", [])
        except Exception as exc:
            return SkillResult(context_data=f"Failed to read #{channel_name}: {exc}")

        if not messages:
            return SkillResult(context_data=f"No messages found in #{channel_name}.")

        # Resolve user names once
        user_cache: dict[str, str] = {}

        async def _user_name(uid: str) -> str:
            if uid in user_cache:
                return user_cache[uid]
            try:
                info = await client.users_info(user=uid)
                name = (
                    info["user"].get("display_name")
                    or info["user"].get("real_name")
                    or info["user"].get("name")
                    or uid
                )
            except Exception:
                name = uid
            user_cache[uid] = name
            return name

        lines = [f"**#{channel_name}** — last {len(messages)} messages\n"]
        for msg in reversed(messages):  # oldest first
            ts = _fmt_ts(msg.get("ts"))
            uid = msg.get("user") or msg.get("bot_id") or "system"
            sender = await _user_name(uid) if msg.get("user") else (msg.get("username") or "bot")
            text = _clean_text(msg.get("text", ""))
            if not text and msg.get("attachments"):
                text = msg["attachments"][0].get("fallback", "(attachment)")
            if not text and msg.get("blocks"):
                # Extract text from blocks
                for block in msg["blocks"]:
                    if block.get("type") == "section":
                        t = block.get("text", {}).get("text", "")
                        if t:
                            text = _clean_text(t)
                            break
            if not text:
                text = "(no text)"
            # Truncate very long messages
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(f"`{ts}` **{sender}**: {text}")

        return SkillResult(context_data="\n".join(lines))

    async def _search(self, params: dict) -> SkillResult:
        query = params.get("query") or params.get("q", "")
        if not query:
            return SkillResult(context_data="Provide a `query` to search for.")
        count = min(int(params.get("limit", 15)), 100)

        client = await self._get_client()
        try:
            resp = await client.search_messages(query=query, count=count)
            matches = resp.get("messages", {}).get("matches", [])
        except Exception as exc:
            return SkillResult(context_data=f"Search failed: {exc}")

        if not matches:
            return SkillResult(context_data=f"No messages found matching `{query}`.")

        lines = [f"**Slack search:** `{query}` — {len(matches)} results\n"]
        for m in matches:
            ts = _fmt_ts(m.get("ts"))
            channel = m.get("channel", {}).get("name", "?")
            sender = m.get("username") or m.get("user", "?")
            text = _clean_text(m.get("text", ""))
            if len(text) > 300:
                text = text[:300] + "…"
            lines.append(f"`{ts}` **#{channel}** | **{sender}**: {text}")

        return SkillResult(context_data="\n".join(lines))

    async def _dm_history(self, params: dict) -> SkillResult:
        user = params.get("user") or params.get("user_id", "")
        limit = min(int(params.get("limit", 20)), 100)

        if not user:
            return SkillResult(context_data="Provide a `user` (name or ID) to fetch DMs with.")

        client = await self._get_client()
        try:
            # Find user ID
            user_id = user
            if not user.startswith("U"):
                resp = await client.users_list()
                for u in resp.get("members", []):
                    if (
                        u.get("name", "").lower() == user.lower()
                        or u.get("real_name", "").lower() == user.lower()
                        or u.get("display_name", "").lower() == user.lower()
                    ):
                        user_id = u["id"]
                        break

            conv = await client.conversations_open(users=user_id)
            dm_channel = conv["channel"]["id"]
            resp = await client.conversations_history(channel=dm_channel, limit=limit)
            messages = resp.get("messages", [])
        except Exception as exc:
            return SkillResult(context_data=f"Failed to fetch DMs with {user}: {exc}")

        if not messages:
            return SkillResult(context_data=f"No DM history found with {user}.")

        lines = [f"**DMs with {user}** — last {len(messages)} messages\n"]
        for msg in reversed(messages):
            ts = _fmt_ts(msg.get("ts"))
            text = _clean_text(msg.get("text", "(no text)"))
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(f"`{ts}`: {text}")

        return SkillResult(context_data="\n".join(lines))
