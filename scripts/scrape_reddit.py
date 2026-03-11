#!/usr/bin/env python3
"""
scrape_reddit.py — standalone Reddit scraper (no Sentinel deps, only httpx).

Usage:
    python scripts/scrape_reddit.py r/python
    python scripts/scrape_reddit.py r/python --limit 10 --time week --output json
    python scripts/scrape_reddit.py r/python --output table

Options:
    --limit    Number of posts to fetch (default: 10)
    --time     Time filter: hour|day|week|month|year|all (default: day)
    --output   Output format: json|table (default: table)
"""

import argparse
import json
import sys
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

_BASE = "https://www.reddit.com"
_USER_AGENT = "sentinel-scraper/1.0"


def fetch_posts(subreddit: str, limit: int, time_filter: str) -> list[dict]:
    url = f"{_BASE}/r/{subreddit}/top.json?limit={limit}&t={time_filter}"
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)

    if resp.status_code == 404:
        print(f"ERROR: r/{subreddit} not found.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code == 403:
        print(f"ERROR: r/{subreddit} is private or quarantined.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code == 429:
        print("ERROR: Reddit rate limit hit. Try again later.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code != 200:
        print(f"ERROR: HTTP {resp.status_code}", file=sys.stderr)
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
                "url": d.get("url", ""),
                "permalink": f"https://reddit.com{d.get('permalink', '')}",
                "author": d.get("author", "[deleted]"),
                "created_utc": datetime.fromtimestamp(
                    d.get("created_utc", 0), tz=timezone.utc
                ).isoformat(),
                "upvote_ratio": d.get("upvote_ratio", 0.0),
            }
        )
    return posts


def print_table(posts: list[dict], subreddit: str) -> None:
    print(f"\n{'='*70}")
    print(f"  r/{subreddit} — Top {len(posts)} Posts")
    print(f"{'='*70}\n")
    for i, p in enumerate(posts, 1):
        print(f"{i:2}. {p['title'][:75]}")
        print(f"    ⬆ {p['score']:,}  💬 {p['num_comments']:,}  by u/{p['author']}")
        print(f"    {p['permalink']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Reddit top posts")
    parser.add_argument("subreddit", help="Subreddit name (with or without r/)")
    parser.add_argument("--limit", type=int, default=10, help="Number of posts")
    parser.add_argument(
        "--time",
        default="day",
        choices=["hour", "day", "week", "month", "year", "all"],
        help="Time filter",
    )
    parser.add_argument(
        "--output", default="table", choices=["json", "table"], help="Output format"
    )
    args = parser.parse_args()

    subreddit = args.subreddit.strip().lstrip("r/")
    posts = fetch_posts(subreddit, args.limit, args.time)

    if args.output == "json":
        print(json.dumps(posts, indent=2))
    else:
        print_table(posts, subreddit)


if __name__ == "__main__":
    main()
