"""
MemoryManager — orchestrates all three memory tiers.

Hot   → Redis    (current session history, 4hr TTL, per interface)
Warm  → Postgres (session summaries, flushed every N turns)
Cold  → Qdrant   (semantic embeddings, global across all sessions)

Cross-interface sharing
-----------------------
Every turn is also cross-posted to a shared *primary session* in Postgres.
All interfaces inject the primary session's warm summary as context, giving
Slack, CLI, and REST API full visibility into each other's recent activity.

Session layout (example):
  slack:U123   — DM/mention history for Slack user U123
  cli:local    — CLI REPL history
  default      — REST API default session
  brain        — PRIMARY: receives cross-posted turns from every interface
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.memory.redis_client import RedisMemory
from app.memory.postgres_memory import PostgresMemory
from app.memory.qdrant_client import QdrantMemory

logger = logging.getLogger(__name__)

# Minimum length for "high-signal" Qdrant cold storage
_HIGH_SIGNAL_MIN_LENGTH = 200


@dataclass
class MemoryContext:
    hot_history: list[dict] = field(default_factory=list)
    warm_summary: str = ""
    cold_matches: list[dict] = field(default_factory=list)
    cross_session_context: str = ""  # from the shared primary session


class MemoryManager:
    def __init__(
        self,
        redis_host: str,
        redis_port: int,
        redis_password: str,
        postgres_dsn: str,
        qdrant_host: str,
        qdrant_port: int,
        qdrant_collection: str,
        flush_interval_turns: int = 10,
        primary_session: str = "brain",
    ) -> None:
        self.redis = RedisMemory()
        self.postgres = PostgresMemory(dsn=postgres_dsn)
        self.qdrant = QdrantMemory(
            host=qdrant_host,
            port=qdrant_port,
            collection=qdrant_collection,
        )
        self._flush_interval = flush_interval_turns
        self._primary_session = primary_session
        # Turn counts per session (resets on restart — acceptable)
        self._turn_counts: dict[str, int] = {}

    # ── Context retrieval ─────────────────────────────────────────────────────

    async def get_full_context(self, session_id: str, message: str) -> MemoryContext:
        """
        Assemble context from all three tiers + the cross-interface primary
        session, all in parallel.
        """
        hot_history = self.redis.get_history(session_id)

        warm_task = asyncio.to_thread(self._get_warm_summary, session_id, hot_history)
        cold_task = self.qdrant.search_relevant_context(message, limit=4)
        cross_task = asyncio.to_thread(self._get_cross_session_context, session_id)

        warm_summary, cold_matches, cross_ctx = await asyncio.gather(warm_task, cold_task, cross_task)

        return MemoryContext(
            hot_history=hot_history,
            warm_summary=warm_summary,
            cold_matches=cold_matches,
            cross_session_context=cross_ctx,
        )

    def _get_warm_summary(self, session_id: str, hot_history: list[dict]) -> str:
        """Return Postgres warm summary for the current session (skip if hot exists)."""
        if hot_history:
            return ""  # Live context is fresh enough
        return self.postgres.get_session_summary(session_id)

    def _get_cross_session_context(self, current_session: str) -> str:
        """
        Return the primary session's warm summary — this carries context from
        every other interface (Slack, CLI, REST) into the current request.
        Skipped when the current session IS the primary session.
        """
        if not self._primary_session or self._primary_session == current_session:
            return ""
        summary = self.postgres.get_session_summary(self._primary_session)
        return summary

    # ── Persistence ───────────────────────────────────────────────────────────

    async def persist_turn(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
        intent: str = "chat",
    ) -> None:
        """
        Persist one turn across all tiers:
          - Redis:           always (hot, per-interface)
          - Postgres:        every flush_interval_turns (warm, per-interface)
          - Primary session: every turn → drives cross-interface context
          - Qdrant:          high-signal turns (cold, global)
        """
        # ── Hot ───────────────────────────────────────────────────────────────
        self.redis.append_turn(session_id, user_msg, assistant_msg)

        self._turn_counts[session_id] = self._turn_counts.get(session_id, 0) + 1
        turn_count = self._turn_counts[session_id]

        # ── Warm (per-interface) ───────────────────────────────────────────────
        if turn_count % self._flush_interval == 0:
            asyncio.create_task(self.flush_session_to_postgres(session_id, intent=intent))

        # ── Cross-interface: feed the primary session ──────────────────────────
        # Every turn from any interface is also stored under the primary session
        # so that cross_session_context stays current for all other interfaces.
        if self._primary_session and self._primary_session != session_id:
            asyncio.create_task(self._cross_post_to_primary(user_msg, assistant_msg, intent))

        # ── Cold (Qdrant — global across sessions) ────────────────────────────
        combined = f"User: {user_msg}\nAssistant: {assistant_msg}"
        if len(combined) >= _HIGH_SIGNAL_MIN_LENGTH:
            asyncio.create_task(
                asyncio.to_thread(
                    self.qdrant.store,
                    session_id,
                    combined,
                    {"intent": intent, "turn": turn_count},
                )
            )

    async def _cross_post_to_primary(self, user_msg: str, assistant_msg: str, intent: str) -> None:
        """
        Store this turn in the primary session's Postgres conversations table
        and periodically regenerate the primary session's warm summary.
        """
        try:
            await asyncio.to_thread(
                self.postgres.store_session,
                self._primary_session,
                [
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ],
                intent,
            )
            # Refresh primary session summary every flush_interval cross-posts
            self._turn_counts[self._primary_session] = self._turn_counts.get(self._primary_session, 0) + 1
            if self._turn_counts[self._primary_session] % self._flush_interval == 0:
                asyncio.create_task(self.flush_session_to_postgres(self._primary_session, intent=intent))
        except Exception as exc:
            logger.debug("Cross-post to primary session failed (non-fatal): %s", exc)

    async def flush_session_to_postgres(
        self,
        session_id: str,
        intent: str = "chat",
    ) -> None:
        """
        Flush the current Redis history to Postgres and generate a summary.
        Called on session end (via SessionHook) or every N turns.
        """
        history = self.redis.get_history(session_id)
        if not history:
            return

        try:
            await asyncio.to_thread(self.postgres.store_session, session_id, history, intent)
            summary = await asyncio.to_thread(self.postgres.generate_summary, history)
            if summary:
                await asyncio.to_thread(
                    self.postgres.store_summary,
                    session_id,
                    summary,
                    len(history) // 2,
                    intent,
                )
            logger.debug("Flushed session %s to Postgres (%d turns)", session_id, len(history) // 2)
        except Exception as exc:
            logger.error("Postgres flush failed for %s: %s", session_id, exc)
