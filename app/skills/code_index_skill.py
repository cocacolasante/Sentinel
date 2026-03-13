"""
CodeIndexSkill — index repo Python symbols into Qdrant for semantic search.

Used internally by PatchGeneratorSkill to find relevant files for a given
test failure message.

Intent: code_index
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from app.config import get_settings
from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)
settings = get_settings()

_COLLECTION = "sentinel_code"
_VECTOR_SIZE = 1536


def _get_embedding(text: str) -> list[float]:
    """Return an embedding vector using OpenAI text-embedding-3-small."""
    if settings.openai_api_key:
        try:
            import openai

            client = openai.OpenAI(api_key=settings.openai_api_key)
            resp = client.embeddings.create(
                model=settings.openai_embedding_model,
                input=text[:8000],
            )
            return resp.data[0].embedding
        except Exception as exc:
            logger.warning("OpenAI embedding failed, using mock: %s", exc)
    return [0.0] * _VECTOR_SIZE


def _extract_symbols(source: str) -> list[str]:
    """Return top-level function/class names from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


class CodeIndexSkill(BaseSkill):
    name = "code_index"
    description = (
        "Index Python source files in the repository into Qdrant for semantic code search. "
        "Used internally by the self-heal pipeline."
    )
    trigger_intents = ["code_index"]

    def is_available(self) -> bool:
        return bool(settings.openai_api_key)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        repo_path = params.get("repo_path", "/root/sentinel-workspace")
        root = Path(repo_path)
        if not root.exists():
            return SkillResult(
                context_data=f"Repo path does not exist: {repo_path}",
                is_error=True,
            )

        # Collect symbols
        symbols: list[tuple[str, list[float]]] = []  # (label, vector)
        file_count = 0

        for py_file in root.rglob("*.py"):
            # Skip test files and venv dirs for the index
            parts = py_file.parts
            if any(p in parts for p in ("venv", ".venv", "node_modules", "__pycache__")):
                continue
            try:
                source = py_file.read_text(errors="replace")
                rel = str(py_file.relative_to(root))
                names = _extract_symbols(source)
                for sym in names:
                    label = f"{rel}::{sym}"
                    symbols.append((label, rel, sym))
                file_count += 1
            except Exception as exc:
                logger.debug("Could not parse %s: %s", py_file, exc)

        if not symbols:
            return SkillResult(context_data=f"No Python symbols found in {repo_path}")

        # Upsert into Qdrant
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http.models import Distance, PointStruct, VectorParams

            client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
            existing = [c.name for c in client.get_collections().collections]
            if _COLLECTION not in existing:
                client.create_collection(
                    collection_name=_COLLECTION,
                    vectors_config=VectorParams(size=_VECTOR_SIZE, distance=Distance.COSINE),
                )

            points: list[PointStruct] = []
            import uuid as _uuid

            for label, rel_path, sym_name in symbols:
                vector = _get_embedding(label)
                points.append(
                    PointStruct(
                        id=str(_uuid.uuid4()),
                        vector=vector,
                        payload={"label": label, "file": rel_path, "symbol": sym_name},
                    )
                )

            # Upsert in batches of 100
            for i in range(0, len(points), 100):
                client.upsert(collection_name=_COLLECTION, points=points[i : i + 100])

        except Exception as exc:
            logger.error("Qdrant upsert failed: %s", exc)
            return SkillResult(context_data=f"Qdrant indexing failed: {exc}", is_error=True)

        return SkillResult(
            context_data=f"Indexed {len(symbols)} symbols from {file_count} files into Qdrant collection '{_COLLECTION}'"
        )


def search_symbols(query: str, limit: int = 5) -> list[dict]:
    """Search Qdrant for symbols relevant to query. Returns list of {file, symbol, score}."""
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        vector = _get_embedding(query)
        results = client.search(
            collection_name=_COLLECTION,
            query_vector=vector,
            limit=limit,
        )
        return [
            {"file": r.payload.get("file", ""), "symbol": r.payload.get("symbol", ""), "score": r.score}
            for r in results
        ]
    except Exception as exc:
        logger.warning("Qdrant symbol search failed: %s", exc)
        return []
