#!/usr/bin/env python3
"""
send_slack.py — fetch Reddit posts and post a digest to a Slack channel.

Requires:
    SLACK_BOT_TOKEN env var (xoxb-...)

Usage:
    SLACK_BOT_TOKEN=xoxb-... python scripts/send_slack.py r/python
    SLACK_BOT_TOKEN=xoxb-... python scripts/send_slack.py r/python --channel sentinel-reddit
    SLACK_BOT_TOKEN=xoxb-... python scripts/send_slack.py r/python --limit 5 --time week
"""

import argparse
import json
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
_USER_AGENT = "sentinel-scraper/1.0"


def fetch_posts(subreddit: str, limit: int, time_filter: str) -> list[dict]:
    url = f"{_REDDIT_BASE}/r/{subreddit}/top.json?limit={limit}&t={time_filter}"
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 404:
        print(f"ERROR: r/{subreddit} not found.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code != 200:
        print(f"ERROR: Reddit HTTP {resp.status_code}", file=sys.stderr)
        sys.exit(1)
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


def post_to_slack(token: str, channel: str, text: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"channel": channel, "text": text}
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(_SLACK_API, headers=headers, json=payload)
    data = resp.json()
    if not data.get("ok"):
        print(f"ERROR: Slack API error: {data.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Posted to #{channel}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Post Reddit digest to Slack")
    parser.add_argument("subreddit", help="Subreddit (with or without r/)")
    parser.add_argument("--channel", default="sentinel-reddit", help="Slack channel name")
    parser.add_argument("--limit", type=int, default=10, help="Number of posts")
    parser.add_argument(
        "--time",
        default="day",
        choices=["hour", "day", "week", "month", "year", "all"],
    )
    args = parser.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN env var is required.", file=sys.stderr)
        sys.exit(1)

    subreddit = args.subreddit.strip().lstrip("r/")
    posts = fetch_posts(subreddit, args.limit, args.time)
    digest = format_digest(subreddit, posts, args.time)
    post_to_slack(token, args.channel, digest)


if __name__ == "__main__":
    main()
