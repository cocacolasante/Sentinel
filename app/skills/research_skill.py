"""ResearchSkill — enriches context via Qdrant semantic search."""

from __future__ import annotations

import json
import logging

from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)


class ResearchSkill(BaseSkill):
    name = "research"
    description = "Research queries — enriches context via semantic memory search"
    trigger_intents = ["research"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        try:
            from app.memory.qdrant_client import QdrantMemory
            from app.config import get_settings
            settings = get_settings()
            qm = QdrantMemory(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                collection=settings.qdrant_collection,
            )
            matches = await qm.search_relevant_context(original_message, limit=5)
            if matches:
                context = "Relevant past context from memory:\n" + json.dumps(matches, indent=2)
                return SkillResult(context_data=context, skill_name=self.name)
        except Exception as exc:
            logger.debug("ResearchSkill Qdrant search skipped: %s", exc)
        return SkillResult(skill_name=self.name)
