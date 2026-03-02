"""
GitHub Integration

Uses the GitHub REST API v3 via httpx (no extra SDK needed).

Operations:
  list_notifications()          — all unread notifications
  list_issues(repo)             — open issues for a repo
  list_prs(repo)                — open PRs for a repo
  get_issue(repo, number)       — single issue detail
  create_issue(repo, title, body, labels) — create a new issue
  list_repos()                  — list user's repos
"""

import logging

import httpx

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

_BASE = "https://api.github.com"
_TIMEOUT = 15.0


class GitHubClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(settings.github_token)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=_BASE,
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
        return self._client

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        r = await self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, json: dict) -> dict:
        r = await self.client.post(path, json=json)
        r.raise_for_status()
        return r.json()

    # ── Public API ────────────────────────────────────────────────────────────

    async def list_notifications(self, only_unread: bool = True) -> list[dict]:
        data = await self._get("/notifications", params={"all": not only_unread})
        return [
            {
                "id":     n.get("id"),
                "repo":   n.get("repository", {}).get("full_name"),
                "type":   n.get("subject", {}).get("type"),
                "title":  n.get("subject", {}).get("title"),
                "reason": n.get("reason"),
                "unread": n.get("unread"),
                "updated_at": n.get("updated_at"),
            }
            for n in data
        ]

    async def list_issues(self, repo: str, state: str = "open") -> list[dict]:
        if not repo:
            repo = settings.github_default_repo
        data = await self._get(f"/repos/{repo}/issues", params={"state": state, "per_page": 20})
        return [
            {
                "number":  i.get("number"),
                "title":   i.get("title"),
                "state":   i.get("state"),
                "author":  i.get("user", {}).get("login"),
                "labels":  [l.get("name") for l in i.get("labels", [])],
                "created": i.get("created_at"),
                "url":     i.get("html_url"),
                "body":    (i.get("body") or "")[:300],
            }
            for i in data
            if not i.get("pull_request")  # exclude PRs from issues list
        ]

    async def list_prs(self, repo: str, state: str = "open") -> list[dict]:
        if not repo:
            repo = settings.github_default_repo
        data = await self._get(f"/repos/{repo}/pulls", params={"state": state, "per_page": 20})
        return [
            {
                "number":    pr.get("number"),
                "title":     pr.get("title"),
                "state":     pr.get("state"),
                "author":    pr.get("user", {}).get("login"),
                "base":      pr.get("base", {}).get("ref"),
                "head":      pr.get("head", {}).get("ref"),
                "draft":     pr.get("draft"),
                "created":   pr.get("created_at"),
                "url":       pr.get("html_url"),
                "body":      (pr.get("body") or "")[:300],
            }
            for pr in data
        ]

    async def get_issue(self, repo: str, number: int) -> dict:
        if not repo:
            repo = settings.github_default_repo
        return await self._get(f"/repos/{repo}/issues/{number}")

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> dict:
        if not repo:
            repo = settings.github_default_repo
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        data = await self._post(f"/repos/{repo}/issues", json=payload)
        return {
            "number": data.get("number"),
            "title":  data.get("title"),
            "url":    data.get("html_url"),
            "state":  data.get("state"),
        }

    async def list_repos(self, per_page: int = 30) -> list[dict]:
        data = await self._get("/user/repos", params={"per_page": per_page, "sort": "updated"})
        return [
            {
                "name":        r.get("full_name"),
                "description": r.get("description", ""),
                "language":    r.get("language"),
                "stars":       r.get("stargazers_count"),
                "updated":     r.get("updated_at"),
                "private":     r.get("private"),
            }
            for r in data
        ]
