"""
ResearchSkill — enriches context via semantic memory and codebase search.

Two-stage strategy:
  1. Qdrant semantic search (past conversations / stored knowledge)
  2. Codebase grep/find on /app (the brain's live source tree) when the
     query looks code-related or Qdrant returned nothing useful.

This ensures the assistant always returns real data instead of saying
"I'll research and update you later" — a pattern that is explicitly
forbidden by the system prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)

# Keywords that suggest the user wants to search the codebase
_CODE_HINTS = re.compile(
    r"\b(file|code|function|class|method|module|skill|intent|dispatcher|route|"
    r"router|integration|config|setting|schema|model|skill|import|def |class |\."
    r"py|\.js|\.ts|\.json|\.yaml|\.yml|\.env|how does|how is|where is|find|grep|"
    r"look at|show me|read|review|codebase|source|implement|logic|where|which file)\b",
    re.IGNORECASE,
)

# Root of the brain codebase inside the container
_APP_ROOT = "/app"
_MAX_GREP = 4_000   # chars
_MAX_FIND = 2_000


async def _grep_codebase(pattern: str, path: str = _APP_ROOT) -> str:
    """Case-insensitive recursive grep over the codebase."""
    try:
        proc = await asyncio.create_subprocess_shell(
            f"grep -rn --include='*.py' --include='*.json' --include='*.yaml' "
            f"--include='*.yml' --include='*.env*' -i {_safe_arg(pattern)} {path} "
            f"2>/dev/null | head -60",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        out = stdout.decode("utf-8", errors="replace").strip()
        if len(out) > _MAX_GREP:
            out = out[:_MAX_GREP] + "\n... (truncated)"
        return out or "(no matches found)"
    except Exception as exc:
        return f"(grep error: {exc})"


async def _find_files(name_hint: str, path: str = _APP_ROOT) -> str:
    """Find files matching a name pattern in the codebase."""
    try:
        proc = await asyncio.create_subprocess_shell(
            f"find {path} -type f -iname '*{_safe_arg(name_hint)}*' 2>/dev/null | head -40",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        out = stdout.decode("utf-8", errors="replace").strip()
        return out[:_MAX_FIND] if out else "(no files found)"
    except Exception as exc:
        return f"(find error: {exc})"


def _safe_arg(s: str) -> str:
    """Minimal shell-safe quoting for a grep/find argument (single-word pattern)."""
    # Strip characters that can't appear in grep patterns passed via shell
    cleaned = re.sub(r"[;|&$`\\\"'<>(){}]", "", s)
    return f'"{cleaned}"'


def _extract_search_term(message: str) -> str:
    """Pull the most useful search term from the user's message."""
    # Strip common question words and keep the substantive part
    cleaned = re.sub(
        r"^(how does|how is|where is|find|show me|read|review|look at|search for|"
        r"what is|tell me about|explain)\s+",
        "", message.strip(), flags=re.IGNORECASE,
    )
    # Take the first 4 significant words
    words = [w for w in cleaned.split() if len(w) > 2][:4]
    return " ".join(words) if words else message[:40]


class ResearchSkill(BaseSkill):
    name = "research"
    description = (
        "Research queries: searches semantic memory (Qdrant) and the live codebase "
        "(grep/find on /app) to return real context. Never defers — always returns "
        "the best available data immediately."
    )
    trigger_intents = ["research"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        parts: list[str] = []

        # ── Stage 1: Qdrant semantic search ──────────────────────────────────
        try:
            from app.memory.qdrant_client import QdrantMemory
            from app.config import get_settings
            settings = get_settings()
            qm = QdrantMemory(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                collection=settings.qdrant_collection,
            )
            matches = await qm.search_relevant_context(original_message, limit=4)
            if matches:
                parts.append(
                    "**Relevant past context from memory:**\n"
                    + json.dumps(matches, indent=2)
                )
        except Exception as exc:
            logger.debug("ResearchSkill Qdrant search skipped: %s", exc)

        # ── Stage 2: Codebase search (always when query looks code-related) ──
        is_code_query = bool(_CODE_HINTS.search(original_message))

        if is_code_query or not parts:
            search_term = _extract_search_term(original_message)

            # Parallel: grep for the term + find files matching it
            grep_task = _grep_codebase(search_term)
            find_task = _find_files(search_term)
            grep_out, find_out = await asyncio.gather(grep_task, find_task)

            if grep_out and grep_out != "(no matches found)":
                parts.append(
                    f"**Codebase grep results for `{search_term}`:**\n```\n{grep_out}\n```"
                )
            if find_out and find_out != "(no files found)":
                parts.append(
                    f"**Files matching `{search_term}`:**\n```\n{find_out}\n```"
                )

            # Also list the top-level app structure so the LLM knows what's available
            if not parts:
                try:
                    proc = await asyncio.create_subprocess_shell(
                        f"find {_APP_ROOT} -type f -name '*.py' | head -80 2>/dev/null",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                    listing = stdout.decode("utf-8", errors="replace").strip()
                    if listing:
                        parts.append(
                            f"**Codebase file listing ({_APP_ROOT}):**\n```\n{listing}\n```"
                        )
                except Exception:
                    pass

        if parts:
            context = "\n\n".join(parts)
            return SkillResult(context_data=context, skill_name=self.name)

        # Absolute fallback — give the LLM explicit guidance so it doesn't defer
        return SkillResult(
            context_data=(
                "[Research: no matching context found in memory or codebase for this query. "
                "Answer from your training knowledge. If you need to read a specific file, "
                f"suggest: 'read file /app/<path>' or 'run: cat /app/<path>'. "
                "DO NOT say you will research and follow up later.]"
            ),
            skill_name=self.name,
        )
