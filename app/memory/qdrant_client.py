"""
QdrantMemory — cold memory tier.

Stores and searches high-signal interaction embeddings in Qdrant.
Uses OpenAI text-embedding-3-small (or a mock fallback if key is missing).
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class QdrantMemory:
    def __init__(self, host: str, port: int, collection: str) -> None:
        self._host = host
        self._port = port
        self._collection = collection
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from qdrant_client import QdrantClient  # type: ignore

            self._client = QdrantClient(host=self._host, port=self._port)
        return self._client

    # ── Init ──────────────────────────────────────────────────────────────────

    async def init_collection(self) -> None:
        """Create the Qdrant collection if it does not exist."""
        try:
            from qdrant_client.http.models import Distance, VectorParams  # type: ignore

            existing = [c.name for c in self.client.get_collections().collections]
            if self._collection not in existing:
                self.client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=settings.qdrant_vector_size,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Qdrant collection '%s' created", self._collection)
            else:
                logger.info("Qdrant collection '%s' already exists", self._collection)
        except Exception as exc:
            logger.warning("Qdrant init_collection failed (non-fatal): %s", exc)

    # ── Embeddings ────────────────────────────────────────────────────────────

    def _get_embedding(self, text: str) -> list[float]:
        """Return an embedding vector; falls back to mock zeros if OpenAI key missing."""
        if settings.openai_api_key:
            try:
                import openai  # type: ignore

                client = openai.OpenAI(api_key=settings.openai_api_key)
                resp = client.embeddings.create(
                    model=settings.openai_embedding_model,
                    input=text[:8000],
                )
                return resp.data[0].embedding
            except Exception as exc:
                logger.warning("OpenAI embedding failed, using mock: %s", exc)
        # Mock: deterministic zeros (no semantic meaning, prevents crashes)
        return [0.0] * settings.qdrant_vector_size

    # ── Store ─────────────────────────────────────────────────────────────────

    def store(
        self,
        session_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        """Embed and store content in Qdrant. Returns the point UUID."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        point_id = str(uuid.uuid4())
        vector = self._get_embedding(content)
        payload = {
            "session_id": session_id,
            "content": content[:2000],
            "content_hash": content_hash,
            **(metadata or {}),
        }
        try:
            from qdrant_client.http.models import PointStruct  # type: ignore

            self.client.upsert(
                collection_name=self._collection,
                points=[PointStruct(id=point_id, vector=vector, payload=payload)],
            )
        except Exception as exc:
            logger.warning("Qdrant store failed: %s", exc)
        return point_id

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Return top-k semantically similar stored interactions."""
        vector = self._get_embedding(query)
        try:
            results = self.client.search(
                collection_name=self._collection,
                query_vector=vector,
                limit=limit,
            )
            return [{"score": r.score, **r.payload} for r in results if r.score > 0.75]
        except Exception as exc:
            logger.warning("Qdrant search failed: %s", exc)
            return []

    async def search_relevant_context(self, query: str, limit: int = 5) -> list[dict]:
        """Async-friendly wrapper around search()."""
        import asyncio

        return await asyncio.to_thread(self.search, query, limit)
