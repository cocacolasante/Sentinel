"""
Reddit Client — async HTTP client wrapping Reddit's public JSON API.

No OAuth required. Uses the public /<subreddit>/top.json and /hot.json endpoints.
Implements exponential back-off retry on 429 responses via tenacity.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_BASE = "https://www.reddit.com"


# ── Exceptions ────────────────────────────────────────────────────────────────


class RedditClientError(Exception):
    pass


class SubredditNotFoundError(RedditClientError):
    pass


class SubredditPrivateError(RedditClientError):
    pass


class RedditRateLimitError(RedditClientError):
    pass


# ── Client ────────────────────────────────────────────────────────────────────


class RedditClient:
    USER_AGENT: str = settings.reddit_user_agent

    def is_accessible(self) -> bool:
        """Quick sync check: returns True if Reddit JSON API is reachable from this IP."""
        try:
            with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                resp = client.get(
                    f"{_BASE}/r/announcements/top.json?limit=1",
                    headers={"User-Agent": self.USER_AGENT},
                )
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_top_posts(
        self, subreddit: str, limit: int = 10, time_filter: str = "day"
    ) -> list[dict]:
        """Fetch top posts from a subreddit."""
        url = f"{_BASE}/r/{subreddit}/top.json?limit={limit}&t={time_filter}"
        return await self._fetch(url, subreddit)

    async def fetch_hot_posts(self, subreddit: str, limit: int = 5) -> list[dict]:
        """Fetch hot posts from a subreddit."""
        url = f"{_BASE}/r/{subreddit}/hot.json?limit={limit}"
        return await self._fetch(url, subreddit)

    @retry(
        retry=retry_if_exception_type(RedditRateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        reraise=True,
    )
    async def _fetch(self, url: str, subreddit: str = "") -> list[dict]:
        headers = {"User-Agent": self.USER_AGENT}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.RequestError as exc:
                raise RedditClientError(f"Network error: {exc}") from exc

        if resp.status_code == 404:
            raise SubredditNotFoundError(
                f"r/{subreddit} not found — check spelling or it may have been banned."
            )
        if resp.status_code == 403:
            # Distinguish: IP-blocked (HTML body) vs private/quarantined (JSON body)
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type or resp.text.strip().startswith("<"):
                raise SubredditPrivateError(
                    f"r/{subreddit}: Reddit is blocking requests from this server IP. "
                    "OAuth credentials (REDDIT_CLIENT_ID/SECRET) are required for server deployments. "
                    "The skill works from home/non-datacenter IPs without OAuth."
                )
            raise SubredditPrivateError(
                f"r/{subreddit} is private or quarantined."
            )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "?")
            raise RedditRateLimitError(
                f"Reddit rate limit hit (Retry-After: {retry_after}s)."
            )
        if resp.status_code != 200:
            raise RedditClientError(
                f"Unexpected HTTP {resp.status_code} from Reddit."
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise RedditClientError(f"Failed to parse Reddit JSON: {exc}") from exc

        children = data.get("data", {}).get("children", [])
        return [self._normalize_post(c) for c in children if c.get("kind") == "t3"]

    @staticmethod
    def _normalize_post(raw: dict) -> dict:
        d = raw.get("data", {})
        return {
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
            "is_self": d.get("is_self", False),
            "subreddit": d.get("subreddit", ""),
        }
