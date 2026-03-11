"""
Remote Log Analysis Skill — analyze error logs from a mesh agent and suggest a patch.
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from app.brain.llm_router import LLMRouter
from app.config import get_settings
from app.db import postgres
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()


class RemoteLogSkill(BaseSkill):
    name = "remote_log_analysis"
    description = "Analyze error logs from a remote Sentinel agent and generate a patch suggestion"
    trigger_intents = ["remote_log_analysis"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        agent_id = params.get("agent_id", "")
        error_event = params.get("error_event", {})

        if not agent_id:
            return SkillResult(context_data="agent_id is required for remote log analysis.")

        try:
            result = await self._analyze_and_patch(agent_id, error_event)
            return SkillResult(context_data=result)
        except Exception as exc:
            logger.error("RemoteLogSkill error: {}", exc)
            return SkillResult(context_data=f"Analysis failed: {exc}", is_error=True)

    async def _analyze_and_patch(self, agent_id: str, error_event: dict) -> str:
        agent = await asyncio.to_thread(
            postgres.execute_one,
            "SELECT app_name, git_sha, sentinel_env FROM mesh_agents WHERE agent_id = %s",
            (agent_id,),
        )

        if not agent:
            return f"Agent `{agent_id}` not found."

        stack_trace = error_event.get("stack_trace", "")
        context_lines = error_event.get("context_lines", [])

        qdrant_context = ""
        try:
            from app.memory.qdrant_client import QdrantMemory
            qm = QdrantMemory(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                collection=settings.qdrant_collection,
            )
            results = await qm.search(
                query=stack_trace[:500],
                session_id=f"agent:{agent_id}:codebase",
                limit=3,
            )
            if results:
                qdrant_context = "\n".join(r.get("content", "") for r in results[:3])
        except Exception as e:
            logger.debug("Qdrant search failed (non-fatal): {}", e)

        prompt = f"""You are a code patch generator. An error occurred in the remote application.

App: {agent['app_name']} (env={agent['sentinel_env']}, sha={agent['git_sha']})

Error stack trace:
{stack_trace}

Context lines from log:
{chr(10).join(context_lines[:20])}

Relevant source files:
{qdrant_context[:2000] if qdrant_context else 'Not available'}

Analyze the error and provide:
1. Root cause explanation
2. Whether a code fix is possible (fixable: true/false)
3. If fixable, provide a unified diff patch

Respond in JSON:
{{
  "fixable": true/false,
  "root_cause": "explanation",
  "diff": "unified diff or empty string",
  "files_changed": ["list of file paths"],
  "explanation": "what the fix does"
}}"""

        router = LLMRouter()
        analysis_raw = await router.chat(
            message=prompt,
            system="You are an expert software engineer. Return only valid JSON.",
            session_id=f"remote_log_{agent_id}",
            agent=None,
        )

        try:
            analysis = json.loads(analysis_raw.strip().strip("```json").strip("```"))
        except Exception:
            return f"Analysis (raw):\n{analysis_raw}"

        if analysis.get("fixable") and analysis.get("diff"):
            return (
                f"**Remote Log Analysis** for `{agent['app_name']}`\n\n"
                f"**Root Cause:** {analysis['root_cause']}\n\n"
                f"**Fix:** {analysis['explanation']}\n\n"
                f"**Files:** {', '.join(analysis['files_changed'])}\n\n"
                f"**Patch ready** — use `dispatch patch to agent {agent_id}` to apply."
            )
        else:
            return (
                f"**Remote Log Analysis** for `{agent['app_name']}`\n\n"
                f"**Root Cause:** {analysis['root_cause']}\n\n"
                f"**Not auto-fixable** — manual review required."
            )
