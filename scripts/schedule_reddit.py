#!/usr/bin/env python3
"""
schedule_reddit.py — cron/n8n wrapper for Reddit digests.

Reads env vars, scrapes each configured subreddit, and posts to Slack.
No Celery or Redis required — suitable for external cron, n8n Execute Command node, etc.

Env vars:
    REDDIT_SUBREDDITS     comma-separated subreddit names (e.g. "python,worldnews")
    SLACK_BOT_TOKEN       xoxb-... Slack bot token
    SLACK_REDDIT_CHANNEL  target Slack channel (default: sentinel-reddit)
    REDDIT_TIME_FILTER    time filter: hour|day|week|month|year|all (default: day)
    REDDIT_LIMIT          number of posts per subreddit (default: 10)

Usage:
    REDDIT_SUBREDDITS=python,worldnews SLACK_BOT_TOKEN=xoxb-... python scripts/schedule_reddit.py
"""

import os
import sys
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

_REDDIT_BASE = "https://www.reddit.com"
_SLACK_API = "https://slack.com/api/chat.postMessage"
_USER_AGENT = "sentinel-scheduler/1.0"


def fetch_posts(subreddit: str, limit: int, time_filter: str) -> list[dict]:
    url = f"{_REDDIT_BASE}/r/{subreddit}/top.json?limit={limit}&t={time_filter}"
    headers = {"User-Agent": _USER_AGENT}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
    except Exception as exc:
        print(f"ERROR fetching r/{subreddit}: {exc}", file=sys.stderr)
        return []

    if resp.status_code != 200:
        print(f"ERROR: HTTP {resp.status_code} for r/{subreddit}", file=sys.stderr)
        return []

    data = resp.json()
    children = data.get("data", {}).get("children", [])
    posts = []
    for c in children:
        if c.get("kind") != "t3":
            continue
        d = c["data"]
        posts.append(
            {
                "title": d.get("title", ""),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "permalink": f"https://reddit.com{d.get('permalink', '')}",
            }
        )
    return posts


def format_digest(subreddit: str, posts: list[dict], time_filter: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    period = {
        "hour": "last hour", "day": "today", "week": "this week",
        "month": "this month", "year": "this year", "all": "all time",
    }.get(time_filter, time_filter)
    lines = [
        f"*📰 Reddit Digest: r/{subreddit}* | {now} | {period}",
        "━" * 40,
        "",
        "🔥 *Top Stories*",
    ]
    for i, p in enumerate(posts, 1):
        lines.append(
            f"{i}. {p['title']} | ⬆️ {p['score']:,} | 💬 {p['num_comments']:,} | {p['permalink']}"
        )
    return "\n".join(lines)


def post_to_slack(token: str, channel: str, text: str) -> bool:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                _SLACK_API, headers=headers, json={"channel": channel, "text": text}
            )
        data = resp.json()
        if not data.get("ok"):
            print(
                f"ERROR: Slack error: {data.get('error', 'unknown')}", file=sys.stderr
            )
            return False
        return True
    except Exception as exc:
        print(f"ERROR: Slack request failed: {exc}", file=sys.stderr)
        return False


def main() -> None:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN is required.", file=sys.stderr)
        sys.exit(1)

    subs_raw = os.environ.get("REDDIT_SUBREDDITS", "")
    if not subs_raw:
        print("ERROR: REDDIT_SUBREDDITS is required (comma-separated).", file=sys.stderr)
        sys.exit(1)

    channel = os.environ.get("SLACK_REDDIT_CHANNEL", "sentinel-reddit")
    time_filter = os.environ.get("REDDIT_TIME_FILTER", "day")
    limit = int(os.environ.get("REDDIT_LIMIT", "10"))

    subreddits = [s.strip().lstrip("r/") for s in subs_raw.split(",") if s.strip()]
    print(f"Processing {len(subreddits)} subreddit(s): {', '.join(subreddits)}")

    success = 0
    for sub in subreddits:
        posts = fetch_posts(sub, limit, time_filter)
        if not posts:
            print(f"  ⚠ No posts for r/{sub} — skipping")
            continue
        digest = format_digest(sub, posts, time_filter)
        ok = post_to_slack(token, channel, digest)
        if ok:
            print(f"  ✅ r/{sub} → #{channel}")
            success += 1
        else:
            print(f"  ❌ r/{sub} failed")

    print(f"\nDone: {success}/{len(subreddits)} digests sent.")
    if success < len(subreddits):
        sys.exit(1)


if __name__ == "__main__":
    main()
