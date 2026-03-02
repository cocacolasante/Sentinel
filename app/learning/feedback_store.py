"""
FeedbackStore — persists interaction ratings to Postgres and seeds Qdrant.

Ratings ≥ 8 are treated as high-quality and stored in Qdrant for future
retrieval (used to bias the brain toward preferred response styles).
"""

from __future__ import annotations

import logging

import psycopg2
import psycopg2.extras

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()


class FeedbackStore:
    def __init__(self, postgres_dsn: str) -> None:
        self._dsn = postgres_dsn

    def _connect(self):
        return psycopg2.connect(self._dsn)

    # ── Store ─────────────────────────────────────────────────────────────────

    def store_rating(
        self,
        session_id: str,
        message_index: int,
        rating: int,
        comment: str | None = None,
        intent: str = "chat",
        source: str = "api",
    ) -> int:
        """Insert a rating and return its ID. Triggers Qdrant seeding if rating >= 8."""
        sql = """
            INSERT INTO interaction_ratings
                (session_id, message_index, rating, comment, intent, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        rating_id = -1
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (session_id, message_index, rating, comment, intent, source))
                    rating_id = cur.fetchone()[0]
                conn.commit()
        except Exception as exc:
            logger.error("FeedbackStore.store_rating failed: %s", exc)
            return -1

        # Seed Qdrant for high-quality interactions
        if rating >= 8:
            self._seed_qdrant(rating_id, session_id, message_index, rating, intent)

        return rating_id

    def _seed_qdrant(
        self,
        rating_id: int,
        session_id: str,
        message_index: int,
        rating: int,
        intent: str,
    ) -> None:
        """Store the rated interaction in Qdrant and mark it in Postgres."""
        try:
            from app.memory.redis_client import RedisMemory
            from app.memory.qdrant_client import QdrantMemory

            history = RedisMemory().get_history(session_id)
            if not history or message_index * 2 >= len(history):
                return

            idx = message_index * 2
            content = (
                f"User: {history[idx]['content']}\n"
                f"Assistant: {history[idx + 1]['content']}"
            ) if idx + 1 < len(history) else history[idx]["content"]

            qm = QdrantMemory(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                collection=settings.qdrant_collection,
            )
            qdrant_id = qm.store(
                session_id=session_id,
                content=content,
                metadata={"rating": rating, "intent": intent, "source": "feedback"},
            )

            # Mark as Qdrant-stored
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE interaction_ratings SET qdrant_stored = TRUE WHERE id = %s",
                        (rating_id,),
                    )
                conn.commit()

        except Exception as exc:
            logger.warning("FeedbackStore Qdrant seed failed: %s", exc)

    # ── Query ─────────────────────────────────────────────────────────────────

    def get_avg_rating(self, intent: str | None = None) -> float:
        """Return average rating overall or filtered by intent."""
        if intent:
            sql = "SELECT AVG(rating) FROM interaction_ratings WHERE intent = %s"
            args = (intent,)
        else:
            sql = "SELECT AVG(rating) FROM interaction_ratings"
            args = ()
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, args)
                    result = cur.fetchone()[0]
            return float(result) if result else 0.0
        except Exception as exc:
            logger.error("FeedbackStore.get_avg_rating failed: %s", exc)
            return 0.0

    def get_high_quality_interactions(self, min_rating: int = 8) -> list[dict]:
        """Return interactions rated at or above min_rating."""
        sql = """
            SELECT session_id, message_index, rating, comment, intent, created_at
            FROM interaction_ratings
            WHERE rating >= %s
            ORDER BY rating DESC, created_at DESC
            LIMIT 100
        """
        try:
            with self._connect() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, (min_rating,))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("FeedbackStore.get_high_quality_interactions failed: %s", exc)
            return []

    def get_summary(self) -> dict:
        """Return aggregate stats across all ratings."""
        sql = """
            SELECT
                COUNT(*)                             AS total_ratings,
                AVG(rating)                          AS avg_rating,
                COUNT(DISTINCT session_id)           AS unique_sessions,
                MODE() WITHIN GROUP (ORDER BY intent) AS top_intent
            FROM interaction_ratings
        """
        try:
            with self._connect() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql)
                    row = dict(cur.fetchone())
            return {
                "total_ratings":    int(row["total_ratings"] or 0),
                "avg_rating":       round(float(row["avg_rating"] or 0), 2),
                "unique_sessions":  int(row["unique_sessions"] or 0),
                "top_intent":       row["top_intent"] or "chat",
            }
        except Exception as exc:
            logger.error("FeedbackStore.get_summary failed: %s", exc)
            return {"total_ratings": 0, "avg_rating": 0.0, "unique_sessions": 0, "top_intent": "chat"}
