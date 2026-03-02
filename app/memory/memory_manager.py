"""
MemoryManager — orchestrates all three memory tiers.

Hot   → Redis (current session history, 4hr TTL)
Warm  → Postgres (session summaries, flushed every N turns)
Cold  → Qdrant (semantic embeddings, stored for high-signal turns)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.memory.redis_client   import RedisMemory
from app.memory.postgres_memory import PostgresMemory
from app.memory.qdrant_client  import QdrantMemory

logger = logging.getLogger(__name__)

# Threshold score for "high-signal" Qdrant storage
_HIGH_SIGNAL_MIN_LENGTH = 200


@dataclass
class MemoryContext:
    hot_history:   list[dict] = field(default_factory=list)
    warm_summary:  str        = ""
    cold_matches:  list[dict] = field(default_factory=list)


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
    ) -> None:
        self.redis    = RedisMemory()
        self.postgres = PostgresMemory(dsn=postgres_dsn)
        self.qdrant   = QdrantMemory(
            host=qdrant_host,
            port=qdrant_port,
            collection=qdrant_collection,
        )
        self._flush_interval = flush_interval_turns
        # Track turn counts per session in-memory (resets on restart — acceptable)
        self._turn_counts: dict[str, int] = {}

    # ── Context retrieval ─────────────────────────────────────────────────────

    async def get_full_context(self, session_id: str, message: str) -> MemoryContext:
        """
        Assemble context from all three tiers in parallel.
        Returns MemoryContext with hot_history, warm_summary, cold_matches.
        """
        # Hot is synchronous and fast — no need to thread
        hot_history = self.redis.get_history(session_id)

        # Warm and cold can be fetched concurrently
        warm_task = asyncio.to_thread(self._get_warm_summary, session_id, hot_history)
        cold_task = self.qdrant.search_relevant_context(message, limit=4)

        warm_summary, cold_matches = await asyncio.gather(warm_task, cold_task)

        return MemoryContext(
            hot_history=hot_history,
            warm_summary=warm_summary,
            cold_matches=cold_matches,
        )

    def _get_warm_summary(self, session_id: str, hot_history: list[dict]) -> str:
        """Return Postgres summary for sessions without hot history (new session)."""
        if hot_history:
            return ""  # We have live context — skip warm
        return self.postgres.get_session_summary(session_id)

    # ── Persistence ───────────────────────────────────────────────────────────

    async def persist_turn(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
        intent: str = "chat",
    ) -> None:
        """
        Persist one conversation turn across tiers:
        - Redis: always (hot)
        - Postgres: every flush_interval_turns (warm)
        - Qdrant: when the exchange is "high signal" (cold)
        """
        # Hot — always
        self.redis.append_turn(session_id, user_msg, assistant_msg)

        # Track turns
        self._turn_counts[session_id] = self._turn_counts.get(session_id, 0) + 1
        turn_count = self._turn_counts[session_id]

        # Warm — flush to Postgres every N turns
        if turn_count % self._flush_interval == 0:
            asyncio.create_task(
                self.flush_session_to_postgres(session_id, intent=intent)
            )

        # Cold — store high-signal turns in Qdrant
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
            await asyncio.to_thread(
                self.postgres.store_session, session_id, history, intent
            )
            summary = await asyncio.to_thread(
                self.postgres.generate_summary, history
            )
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
