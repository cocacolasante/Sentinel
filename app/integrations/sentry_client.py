"""
Sentry API Integration

Read and manage Sentry issues via the Sentry REST API.

Required .env vars:
  SENTRY_AUTH_TOKEN  — Sentry user/org auth token (Settings > Auth Tokens)
  SENTRY_ORG         — Sentry organization slug
  SENTRY_PROJECT     — default Sentry project slug (optional)

Docs: https://docs.sentry.io/api/
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SENTRY_BASE = "https://sentry.io/api/0"


class SentryClient:
    def __init__(self) -> None:
        self._token = settings.sentry_auth_token
        self._org = settings.sentry_org
        self._project = settings.sentry_project

    def is_configured(self) -> bool:
        return bool(self._token and self._org)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ── Sync internals ────────────────────────────────────────────────────────

    def _list_issues_sync(
        self,
        project: str | None = None,
        query: str = "is:unresolved",
        limit: int = 25,
        sort: str = "date",
        stats_period: str | None = None,
    ) -> list[dict]:
        proj = project or self._project
        if proj:
            url = f"{SENTRY_BASE}/projects/{self._org}/{proj}/issues/"
        else:
            url = f"{SENTRY_BASE}/organizations/{self._org}/issues/"

        params: dict = {"query": query, "limit": limit, "sort": sort}
        if stats_period:
            params["statsPeriod"] = stats_period

        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers=self._headers(), params=params)
            r.raise_for_status()
            return [self._format_issue(i) for i in r.json()]

    def _get_issue_sync(self, issue_id: str) -> dict:
        url = f"{SENTRY_BASE}/issues/{issue_id}/"
        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            return self._format_issue(r.json())

    def _update_issue_sync(self, issue_id: str, **kwargs) -> dict:
        url = f"{SENTRY_BASE}/issues/{issue_id}/"
        with httpx.Client(timeout=15) as client:
            r = client.put(url, headers=self._headers(), json=kwargs)
            r.raise_for_status()
            return self._format_issue(r.json())

    def _add_note_sync(self, issue_id: str, text: str) -> dict:
        url = f"{SENTRY_BASE}/issues/{issue_id}/notes/"
        with httpx.Client(timeout=15) as client:
            r = client.post(url, headers=self._headers(), json={"text": text})
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _format_issue(raw: dict) -> dict:
        proj = raw.get("project", {})
        return {
            "id": raw.get("id", ""),
            "title": raw.get("title", ""),
            "level": raw.get("level", "error"),
            "status": raw.get("status", "unresolved"),
            "project": proj.get("slug", "") if isinstance(proj, dict) else str(proj),
            "platform": raw.get("platform", ""),
            "count": raw.get("count", 0),
            "first_seen": raw.get("firstSeen", ""),
            "last_seen": raw.get("lastSeen", ""),
            "permalink": raw.get("permalink", ""),
            "assigned_to": (raw.get("assignedTo") or {}).get("email", "")
            if isinstance(raw.get("assignedTo"), dict)
            else "",
            "culprit": raw.get("culprit", ""),
        }

    # ── Public async API ──────────────────────────────────────────────────────

    async def list_issues(
        self,
        project: str | None = None,
        query: str = "is:unresolved",
        limit: int = 25,
        sort: str = "date",
        stats_period: str | None = None,
    ) -> list[dict]:
        return await asyncio.to_thread(
            self._list_issues_sync, project, query, limit, sort, stats_period
        )

    async def get_issue(self, issue_id: str) -> dict:
        return await asyncio.to_thread(self._get_issue_sync, issue_id)

    async def resolve_issue(self, issue_id: str) -> dict:
        return await asyncio.to_thread(self._update_issue_sync, issue_id, status="resolved")

    async def ignore_issue(self, issue_id: str) -> dict:
        return await asyncio.to_thread(self._update_issue_sync, issue_id, status="ignored")

    async def assign_issue(self, issue_id: str, assignee: str) -> dict:
        return await asyncio.to_thread(self._update_issue_sync, issue_id, assignedTo=assignee)

    async def add_note(self, issue_id: str, text: str) -> dict:
        return await asyncio.to_thread(self._add_note_sync, issue_id, text)
