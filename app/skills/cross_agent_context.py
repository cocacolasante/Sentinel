"""
Cross-Agent Context Skill — fleet-wide semantic search across all agent codebases.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from app.config import get_settings
from app.db import postgres
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()


class CrossAgentContextSkill(BaseSkill):
    name = "cross_agent_query"
    description = "Fleet-wide query: find similar errors or code patterns across all Sentinel agents"
    trigger_intents = ["cross_agent_query"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        query = params.get("query", original_message)
        namespace_filter = params.get("namespace_filter", "all")

        try:
            return await self._search(query, namespace_filter)
        except Exception as exc:
            logger.error("CrossAgentContextSkill error: {}", exc)
            return SkillResult(context_data=f"Cross-agent search failed: {exc}", is_error=True)

    async def _search(self, query: str, namespace_filter: str) -> SkillResult:
        agents = await asyncio.to_thread(
            postgres.execute,
            "SELECT agent_id, app_name FROM mesh_agents WHERE is_revoked = FALSE",
        )

        if not agents:
            return SkillResult(context_data="No mesh agents registered.")

        results = []
        try:
            from app.memory.qdrant_client import QdrantMemory
            qm = QdrantMemory(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                collection=settings.qdrant_collection,
            )

            for agent in agents:
                agent_id = str(agent["agent_id"])
                app_name = agent["app_name"]

                if namespace_filter not in ("all", agent_id, app_name):
                    continue

                hits = await qm.search(
                    query=query,
                    session_id=f"agent:{agent_id}:codebase",
                    limit=2,
                )
                for h in hits:
                    results.append({
                        "agent_id": agent_id,
                        "app_name": app_name,
                        "content": h.get("content", "")[:300],
                        "score": h.get("score", 0),
                    })
        except Exception as e:
            logger.debug("Qdrant cross-agent search failed (non-fatal): {}", e)

        if not results:
            patches = await asyncio.to_thread(
                postgres.execute,
                """
                SELECT ma.app_name, mp.triggered_by, mp.status, mp.created_at
                FROM mesh_patches mp
                JOIN mesh_agents ma ON ma.agent_id = mp.agent_id
                WHERE mp.triggered_by = 'log_error'
                ORDER BY mp.created_at DESC LIMIT 5
                """,
            )

            if patches:
                lines = ["**Recent Log-Error Patches Across Fleet:**"]
                for p in patches:
                    lines.append(
                        f"- `{p['app_name']}` | {p['triggered_by']} | "
                        f"status={p['status']} | {p['created_at']}"
                    )
                return SkillResult(context_data="\n".join(lines))

            return SkillResult(
                context_data=(
                    f"No cross-agent results found for: '{query}'. "
                    f"Agents may not have indexed codebases yet."
                )
            )

        results.sort(key=lambda x: x["score"], reverse=True)
        lines = [f"**Cross-Agent Search Results** for '{query}' ({len(results)} hits)\n"]
        for r in results[:5]:
            lines.append(
                f"**{r['app_name']}** (agent=`{r['agent_id'][:8]}...`)\n"
                f"```\n{r['content']}\n```"
            )
        return SkillResult(context_data="\n".join(lines))
