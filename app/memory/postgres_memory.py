"""
PostgresMemory — warm memory tier.

Stores session conversation turns, generates Haiku summaries every N turns,
and retrieves summaries for session context injection.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import anthropic
import psycopg2
import psycopg2.extras

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()


class PostgresMemory:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ai: anthropic.Anthropic | None = None

    @property
    def _ai_client(self) -> anthropic.Anthropic:
        if self._ai is None:
            self._ai = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._ai

    def _connect(self):
        return psycopg2.connect(self._dsn)

    # ── Store / retrieve turns ────────────────────────────────────────────────

    def store_session(
        self,
        session_id: str,
        turns: list[dict],
        intent: str = "chat",
    ) -> None:
        """Bulk-insert conversation turns into the `conversations` table."""
        if not turns:
            return
        rows = [
            (session_id, t["role"], t["content"], intent)
            for t in turns
        ]
        sql = """
            INSERT INTO conversations (session_id, role, content, intent)
            VALUES %s
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(cur, sql, rows)
                conn.commit()
        except Exception as exc:
            logger.error("PostgresMemory.store_session failed: %s", exc)

    def get_recent_sessions(self, session_id: str, limit: int = 40) -> list[dict]:
        """Return the last `limit` turns for a session from Postgres."""
        sql = """
            SELECT role, content
            FROM conversations
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """
        try:
            with self._connect() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, (session_id, limit))
                    rows = cur.fetchall()
            return [dict(r) for r in reversed(rows)]
        except Exception as exc:
            logger.error("PostgresMemory.get_recent_sessions failed: %s", exc)
            return []

    # ── Summaries ─────────────────────────────────────────────────────────────

    def get_session_summary(self, session_id: str) -> str:
        """Return the latest stored summary for a session, or empty string."""
        sql = """
            SELECT summary FROM session_summaries
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (session_id,))
                    row = cur.fetchone()
            return row[0] if row else ""
        except Exception as exc:
            logger.error("PostgresMemory.get_session_summary failed: %s", exc)
            return ""

    def store_summary(
        self,
        session_id: str,
        summary: str,
        turn_count: int,
        intent_mix: str = "",
    ) -> None:
        """Persist a generated summary for a session."""
        sql = """
            INSERT INTO session_summaries (session_id, summary, turn_count, intent_mix)
            VALUES (%s, %s, %s, %s)
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (session_id, summary, turn_count, intent_mix))
                conn.commit()
        except Exception as exc:
            logger.error("PostgresMemory.store_summary failed: %s", exc)

    def generate_summary(self, turns: list[dict]) -> str:
        """Use Haiku to generate a concise session summary from turn history."""
        if not turns:
            return ""
        conversation_text = "\n".join(
            f"{t['role'].upper()}: {t['content']}" for t in turns[-20:]
        )
        try:
            response = self._ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=(
                    "You are a session summarizer. Produce a concise 2-3 sentence summary "
                    "of the key topics, decisions, and outcomes from the conversation below. "
                    "Write in third person. Do not add any preamble."
                ),
                messages=[{"role": "user", "content": conversation_text}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            logger.error("Summary generation failed: %s", exc)
            return ""
