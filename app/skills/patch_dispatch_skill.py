"""
Patch Dispatch Skill — sign and dispatch a code patch to a remote Sentinel agent.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time

import redis.asyncio as aioredis
from loguru import logger

from app.config import get_settings
from app.db import postgres
from app.integrations.slack_notifier import post_alert
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()


class PatchDispatchSkill(BaseSkill):
    name = "patch_dispatch"
    description = "Sign and dispatch a code patch to a remote Sentinel Mesh Agent. Production agents require Slack approval before patching. Use when Anthony says 'dispatch patch to agent', 'send code patch to [server]', 'apply fix to remote agent', 'push patch to [app_name]', or 'deploy hotfix to [agent]'. Requires CRITICAL approval. NOT for: applying patches to the local Sentinel server (use repo_write), or running commands on agents (use agent_exec)."
    trigger_intents = ["patch_dispatch"]
    approval_category = ApprovalCategory.CRITICAL

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        agent_id = params.get("agent_id", "")
        diff_text = params.get("diff_text", "")
        triggered_by = params.get("triggered_by", "manual")

        if not agent_id:
            return SkillResult(context_data="agent_id is required.")
        if not diff_text:
            return SkillResult(context_data="diff_text is required.")

        try:
            return await self._dispatch(agent_id, diff_text, triggered_by, params)
        except Exception as exc:
            logger.error("PatchDispatchSkill error: {}", exc)
            return SkillResult(context_data=f"Patch dispatch failed: {exc}", is_error=True)

    async def _dispatch(
        self, agent_id: str, diff_text: str, triggered_by: str, params: dict
    ) -> SkillResult:
        agent = await asyncio.to_thread(
            postgres.execute_one,
            "SELECT app_name, hmac_secret, sentinel_env, is_connected FROM mesh_agents WHERE agent_id = %s AND is_revoked = FALSE",
            (agent_id,),
        )
        if not agent:
            return SkillResult(context_data=f"Agent `{agent_id}` not found or revoked.")

        if not agent["is_connected"]:
            return SkillResult(
                context_data=f"Agent `{agent_id}` is offline — cannot dispatch patch."
            )

        files_changed = params.get("files_changed", [])
        patch_row = await asyncio.to_thread(
            postgres.execute_one,
            """
            INSERT INTO mesh_patches (agent_id, triggered_by, diff_text, files_changed, status)
            VALUES (%s, %s, %s, %s, 'pending')
            RETURNING patch_id
            """,
            (agent_id, triggered_by, diff_text, json.dumps(files_changed)),
        )
        patch_id = str(patch_row["patch_id"])

        is_production = agent["sentinel_env"] == "production"
        approved = not is_production

        if is_production:
            approved = await self._require_slack_approval(
                agent_id, patch_id, agent["app_name"], diff_text
            )
            if not approved:
                return SkillResult(context_data=f"Patch `{patch_id}` was rejected or timed out.")

        await self._dispatch_to_agent(
            agent_id, patch_id, diff_text, agent["hmac_secret"], files_changed, approved=approved
        )

        await asyncio.to_thread(
            postgres.execute,
            "UPDATE mesh_patches SET status = 'dispatched', updated_at = NOW() WHERE patch_id = %s",
            (patch_id,),
        )

        return SkillResult(
            context_data=(
                f"**Patch Dispatched** ✅\n"
                f"agent=`{agent['app_name']}` | patch_id=`{patch_id}`\n"
                f"env={agent['sentinel_env']} | approved={approved}"
            )
        )

    def _sign_patch(self, patch_id: str, diff: str, secret_hash: str) -> str:
        ts = time.time()
        payload = json.dumps(
            {"patch_id": patch_id, "diff": diff}, sort_keys=True, separators=(",", ":")
        )
        canonical = f"{ts}:PATCH_INSTRUCTION:{payload}"
        return hmac.new(secret_hash.encode(), canonical.encode(), "sha256").hexdigest()

    async def _require_slack_approval(
        self, agent_id: str, patch_id: str, app_name: str, diff: str
    ) -> bool:
        approval_key = f"sentinel:agent:patch_approval:{patch_id}"
        redis = aioredis.from_url(
            f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/0",
            decode_responses=True,
        )
        try:
            await redis.set(approval_key, "pending", ex=1800)
            msg = (
                f"🔐 *Patch Approval Required* — production agent\n"
                f"app=`{app_name}` | agent=`{agent_id}`\n"
                f"patch_id=`{patch_id}`\n\n"
                f"```diff\n{diff[:1500]}\n```\n\n"
                f"Reply `approve {patch_id}` or `reject {patch_id}` in this channel."
            )
            await post_alert(msg, settings.slack_agents_channel)

            for _ in range(360):  # 30 min × 5s
                await asyncio.sleep(5)
                decision = await redis.get(approval_key)
                if decision == "approved":
                    return True
                if decision == "rejected":
                    return False
            return False
        finally:
            await redis.aclose()

    async def _dispatch_to_agent(
        self,
        agent_id: str,
        patch_id: str,
        diff_text: str,
        secret_hash: str,
        files_changed: list,
        approved: bool,
    ):
        ts = time.time()
        payload = {
            "patch_id": patch_id,
            "diff": diff_text,
            "files_changed": files_changed,
            "approved": approved,
        }
        sig = self._sign_patch(patch_id, diff_text, secret_hash)
        instruction = json.dumps({
            "type": "PATCH_INSTRUCTION",
            "payload": payload,
            "patch_sig": sig,
            "ts": ts,
        })

        redis = aioredis.from_url(
            f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/0",
            decode_responses=True,
        )
        try:
            await redis.rpush(f"sentinel:agent:cmd:{agent_id}", instruction)
            await redis.expire(f"sentinel:agent:cmd:{agent_id}", 3600)
        finally:
            await redis.aclose()

        logger.info("Patch dispatched | agent={} patch={}", agent_id, patch_id)
