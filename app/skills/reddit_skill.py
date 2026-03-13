"""
Reddit Skills

RedditReadSkill   — fetch top/hot posts, AI-summarize, post to sentinel-reddit
RedditScheduleSkill — manage recurring digest schedules stored in Redis
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)

_REDIS_SCHEDULES_KEY = "sentinel:reddit:schedules"


# ── RedditReadSkill ───────────────────────────────────────────────────────────


class RedditReadSkill(BaseSkill):
    name = "reddit_read"
    description = (
        "Read and monitor Reddit content: fetch posts from subreddits, search for keywords, "
        "check hot/new/top content, read comments. Use when Anthony says 'check Reddit', "
        "'what's on r/[subreddit]', 'search Reddit for [topic]', 'any posts about [keyword]', "
        "or 'show me top Reddit posts'. NOT for: posting or scheduling Reddit content "
        "(use reddit_schedule)."
    )
    trigger_intents = ["reddit_read"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.reddit_client import (
            RedditClient,
            SubredditNotFoundError,
            SubredditPrivateError,
            RedditRateLimitError,
            RedditClientError,
        )
        from app.integrations.slack_notifier import post_alert
        from app.config import get_settings

        s = get_settings()
        subreddit = (params.get("subreddit") or "").strip().lstrip("r/")
        if not subreddit:
            return SkillResult(
                context_data="Please specify a subreddit. Example: `summarize r/python`",
                is_error=True,
            )

        limit = int(params.get("limit", 10))
        time_filter = params.get("time_filter", "day")
        channel = params.get("channel") or s.slack_reddit_channel

        client = RedditClient()
        try:
            top_posts = await client.fetch_top_posts(subreddit, limit=limit, time_filter=time_filter)
            hot_posts = await client.fetch_hot_posts(subreddit, limit=5)
        except SubredditNotFoundError as exc:
            msg = f"❌ *r/{subreddit}* — {exc}"
            try:
                await post_alert(msg, channel)
            except Exception:
                pass
            return SkillResult(context_data=str(exc), is_error=True)
        except SubredditPrivateError as exc:
            msg = f"🔒 *r/{subreddit}* — {exc}"
            try:
                await post_alert(msg, channel)
            except Exception:
                pass
            return SkillResult(context_data=str(exc), is_error=True)
        except RedditRateLimitError as exc:
            msg = f"⏳ Reddit rate limit exhausted for r/{subreddit}: {exc}"
            try:
                await post_alert(msg, s.slack_alert_channel)
            except Exception:
                pass
            return SkillResult(context_data=str(exc), is_error=True)
        except RedditClientError as exc:
            return SkillResult(context_data=f"Reddit error: {exc}", is_error=True)

        if not top_posts and not hot_posts:
            return SkillResult(context_data=f"r/{subreddit} returned no posts.")

        ai_summary = await self._ai_summary(subreddit, top_posts or hot_posts)
        viral = self._detect_viral(hot_posts)
        digest = self._format_digest(subreddit, top_posts, ai_summary, viral, time_filter)

        try:
            await post_alert(digest, channel)
        except Exception as exc:
            logger.error("Failed to post Reddit digest to Slack: %s", exc)

        return SkillResult(context_data=digest)

    @staticmethod
    async def _ai_summary(subreddit: str, posts: list[dict]) -> str:
        import anthropic
        from app.config import get_settings

        s = get_settings()
        if not s.anthropic_api_key or not posts:
            return ""

        titles = "\n".join(
            f"- {p['title']} (⬆{p['score']:,})" for p in posts[:10]
        )
        prompt = (
            f"You are summarizing the top Reddit posts from r/{subreddit}. "
            f"Write 2-3 concise sentences capturing the main themes and most significant stories. "
            f"Be factual and informative. No bullet points — flowing prose.\n\nPosts:\n{titles}"
        )
        try:
            client = anthropic.Anthropic(api_key=s.anthropic_api_key)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:
            logger.warning("Reddit AI summary failed: %s", exc)
            return ""

    @staticmethod
    def _detect_viral(hot_posts: list[dict]) -> dict | None:
        for p in hot_posts:
            if p.get("score", 0) > 1000 or p.get("upvote_ratio", 0) > 0.95:
                return p
        return None

    @staticmethod
    def _format_digest(
        subreddit: str,
        posts: list[dict],
        ai_summary: str,
        viral: dict | None,
        time_filter: str,
    ) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        period_label = {
            "hour": "last hour", "day": "today", "week": "this week",
            "month": "this month", "year": "this year", "all": "all time",
        }.get(time_filter, time_filter)

        lines = [
            f"*📰 Reddit Digest: r/{subreddit}* | {now} | {period_label}",
            "━" * 40,
            "",
        ]

        if posts:
            lines.append("🔥 *Top Stories*")
            for i, p in enumerate(posts[:10], 1):
                score = f"{p['score']:,}"
                comments = f"{p['num_comments']:,}"
                lines.append(
                    f"{i}. {p['title']} | ⬆️ {score} | 💬 {comments} | {p['permalink']}"
                )
            lines.append("")

        if ai_summary:
            lines += ["🤖 *AI Summary*", ai_summary, ""]

        if viral:
            lines += [
                "⚡ *Breaking/Viral*",
                f"• {viral['title']} | ⬆️ {viral['score']:,} | {viral['permalink']}",
                "",
            ]

        return "\n".join(lines)


# ── RedditScheduleSkill ───────────────────────────────────────────────────────


class RedditScheduleSkill(BaseSkill):
    name = "reddit_schedule"
    description = (
        "Schedule and publish Reddit posts to a subreddit at a specified time. Use when Anthony "
        "says 'post to Reddit', 'schedule a Reddit post', 'submit to r/[subreddit]', or "
        "'post [content] to Reddit at [time]'. NOT for: reading Reddit (use reddit_read) or "
        "general social content (use social_caption)."
    )
    trigger_intents = ["reddit_schedule"]
    approval_category = ApprovalCategory.STANDARD

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = (params.get("action") or "list").lower()

        if action in ("add", "set"):
            return await self._add_schedule(params)
        if action == "list":
            return await self._list_schedules()
        if action in ("remove", "delete"):
            return await self._remove_schedule(params)
        if action == "pause":
            return await self._toggle_schedule(params, enabled=False)
        if action == "resume":
            return await self._toggle_schedule(params, enabled=True)

        return SkillResult(
            context_data="Unknown action. Use: add, list, remove, pause, resume"
        )

    async def _add_schedule(self, params: dict) -> SkillResult:
        from croniter import croniter

        subreddit = (params.get("subreddit") or "").strip().lstrip("r/")
        if not subreddit:
            return SkillResult(
                context_data="Specify a `subreddit` to schedule.", is_error=True
            )

        cron = params.get("cron", "")
        # Build cron from natural params if not provided
        if not cron:
            hour = params.get("hour", 8)
            minute = params.get("minute", 0)
            cron = f"{minute} {hour} * * *"

        if not croniter.is_valid(cron):
            return SkillResult(
                context_data=f"Invalid cron expression: `{cron}`. Example: `0 8 * * *` for daily at 08:00 UTC.",
                is_error=True,
            )

        from app.config import get_settings
        s = get_settings()
        channel = params.get("channel") or s.slack_reddit_channel
        time_filter = params.get("time_filter", "day")
        limit = int(params.get("limit", 10))

        schedules = _load_schedules()
        # Deduplicate: update existing entry for same subreddit+channel
        for entry in schedules:
            if entry.get("subreddit") == subreddit and entry.get("channel") == channel:
                entry.update(
                    {
                        "cron": cron,
                        "time_filter": time_filter,
                        "limit": limit,
                        "enabled": True,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                _save_schedules(schedules)
                return SkillResult(
                    context_data=(
                        f"✅ Updated schedule for r/{subreddit} → {channel} "
                        f"| cron: `{cron}` | filter: {time_filter}"
                    )
                )

        new_entry = {
            "id": str(uuid.uuid4())[:8],
            "subreddit": subreddit,
            "cron": cron,
            "channel": channel,
            "time_filter": time_filter,
            "limit": limit,
            "enabled": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        schedules.append(new_entry)
        _save_schedules(schedules)
        return SkillResult(
            context_data=(
                f"✅ Scheduled r/{subreddit} digest → #{channel} "
                f"| cron: `{cron}` | filter: {time_filter} | id: {new_entry['id']}"
            )
        )

    async def _list_schedules(self) -> SkillResult:
        schedules = _load_schedules()
        if not schedules:
            return SkillResult(context_data="No Reddit digest schedules configured.")

        lines = [f"*📅 Reddit Digest Schedules* ({len(schedules)} total)\n"]
        for s in schedules:
            status = "▶️" if s.get("enabled", True) else "⏸️"
            lines.append(
                f"{status} `{s['id']}` r/{s['subreddit']} → #{s['channel']} "
                f"| `{s['cron']}` | filter: {s.get('time_filter', 'day')}"
            )
        return SkillResult(context_data="\n".join(lines))

    async def _remove_schedule(self, params: dict) -> SkillResult:
        subreddit = (params.get("subreddit") or "").strip().lstrip("r/")
        entry_id = params.get("id", "")
        schedules = _load_schedules()
        before = len(schedules)
        schedules = [
            s for s in schedules
            if s.get("id") != entry_id and s.get("subreddit") != subreddit
        ]
        if len(schedules) == before:
            return SkillResult(
                context_data=f"No schedule found for r/{subreddit or entry_id}.", is_error=True
            )
        _save_schedules(schedules)
        removed = before - len(schedules)
        return SkillResult(context_data=f"✅ Removed {removed} schedule(s).")

    async def _toggle_schedule(self, params: dict, enabled: bool) -> SkillResult:
        subreddit = (params.get("subreddit") or "").strip().lstrip("r/")
        entry_id = params.get("id", "")
        schedules = _load_schedules()
        changed = 0
        for s in schedules:
            if s.get("id") == entry_id or s.get("subreddit") == subreddit:
                s["enabled"] = enabled
                changed += 1
        if not changed:
            return SkillResult(
                context_data=f"No schedule found for r/{subreddit or entry_id}.", is_error=True
            )
        _save_schedules(schedules)
        verb = "resumed" if enabled else "paused"
        return SkillResult(context_data=f"✅ {changed} schedule(s) {verb}.")


# ── Redis helpers ─────────────────────────────────────────────────────────────


def _load_schedules() -> list[dict]:
    try:
        from app.memory.redis_client import RedisMemory
        r = RedisMemory().client
        raw = r.get(_REDIS_SCHEDULES_KEY)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to load Reddit schedules from Redis: %s", exc)
    return []


def _save_schedules(schedules: list[dict]) -> None:
    try:
        from app.memory.redis_client import RedisMemory
        r = RedisMemory().client
        r.set(_REDIS_SCHEDULES_KEY, json.dumps(schedules))
    except Exception as exc:
        logger.warning("Failed to save Reddit schedules to Redis: %s", exc)
