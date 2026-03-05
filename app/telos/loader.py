"""
TelosLoader — assembles personal context from telos/*.md files into a system prompt block.

Files are loaded from `telos_dir`, tagged with their filename, and joined into a
single TELOS context block that gets injected into every LLM call.

Cache: in-memory, invalidated after `cache_ttl_seconds` (default 5 min). The
`reload()` method forces an immediate refresh.
"""

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_ORDER = [
    "mission.md",
    "context.md",
    "goals.md",
    "projects.md",
    "beliefs.md",
    "strategies.md",
    "style.md",
]


class TelosLoader:
    def __init__(self, telos_dir: str, cache_ttl_seconds: int = 300) -> None:
        self._dir = Path(telos_dir)
        self._ttl = cache_ttl_seconds
        self._cache: str | None = None
        self._loaded_at: float = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_block(self) -> str:
        """Return the TELOS context block, refreshing from disk if stale."""
        if self._cache is None or (time.time() - self._loaded_at) > self._ttl:
            self._cache = self._build()
            self._loaded_at = time.time()
        return self._cache

    def reload(self) -> list[str]:
        """Force reload from disk. Returns list of loaded filenames."""
        self._cache = self._build()
        self._loaded_at = time.time()
        return self._loaded_files()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _build(self) -> str:
        if not self._dir.exists():
            logger.warning("Telos directory not found: %s", self._dir)
            return ""

        sections: list[str] = []
        for filename in self._file_order():
            path = self._dir / filename
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
                tag = filename.removesuffix(".md").upper()
                sections.append(f"[TELOS: {tag}]\n{content}")
            except Exception as exc:
                logger.warning("Failed to read telos file %s: %s", filename, exc)

        if not sections:
            return ""

        return "--- PERSONAL CONTEXT (TELOS) ---\n\n" + "\n\n".join(sections) + "\n\n--- END TELOS ---"

    def _file_order(self) -> list[str]:
        """Return files in priority order, then any extras alphabetically."""
        ordered = [f for f in _DEFAULT_ORDER if (self._dir / f).exists()]
        extras = sorted(f.name for f in self._dir.glob("*.md") if f.name not in _DEFAULT_ORDER)
        return ordered + extras

    def _loaded_files(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [f.name for f in sorted(self._dir.glob("*.md"))]
