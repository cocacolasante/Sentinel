"""
Agent Registry Skill — query and manage Sentinel Mesh Agents.
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from app.config import get_settings
from app.db import postgres
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()


class AgentRegistrySkill(BaseSkill):
    name = "agent_registry"
    description = (
        "View and manage registered Sentinel Mesh Agents: list all agents, check connection "
        "status, see which servers are online, view agent details and last heartbeat. Use when "
        "Anthony says 'list agents', 'which servers are connected', 'show mesh agents', "
        "'is [server] online', or 'check agent status'. NOT for: executing commands on agents "
        "(use agent_exec) or dispatching code patches (use patch_dispatch)."
    )
    trigger_intents = ["agent_registry"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "list")
        try:
            if action == "list":
                return await self._list(params)
            elif action == "get":
                return await self._get(params)
            elif action == "fleet_summary":
                return await self._fleet_summary()
            elif action == "health":
                return await self._health(params)
            else:
                return await self._list(params)
        except Exception as exc:
            logger.error("AgentRegistrySkill error: {}", exc)
            return SkillResult(
                context_data=f"Error querying mesh agents: {exc}",
                is_error=True,
            )

    async def _list(self, params: dict) -> SkillResult:
        env_filter = params.get("env")
        connected_filter = params.get("connected")

        conditions = ["is_revoked = FALSE"]
        args: list = []
        if env_filter:
            args.append(env_filter)
            conditions.append("sentinel_env = %s")
        if connected_filter is not None:
            args.append(connected_filter)
            conditions.append("is_connected = %s")

        where = " AND ".join(conditions)
        rows = await asyncio.to_thread(
            postgres.execute,
            f"""
            SELECT agent_id, app_name, hostname, ip_address, sentinel_env,
                   agent_version, git_sha, is_connected, last_seen
            FROM mesh_agents WHERE {where}
            ORDER BY is_connected DESC, registered_at DESC
            """,
            args or None,
        )

        if not rows:
            return SkillResult(context_data="No mesh agents registered.")

        lines = [f"**Sentinel Mesh Agents** ({len(rows)} total)\n"]
        for r in rows:
            status = "🟢 online" if r["is_connected"] else "🔴 offline"
            last_seen_raw = r.get("last_seen")
            last_seen = last_seen_raw.strftime("%Y-%m-%d %H:%M UTC") if last_seen_raw else "never"
            lines.append(
                f"- **{r['app_name']}** ({r['hostname'] or 'unknown'}) — {status}\n"
                f"  env={r['sentinel_env']} | sha={r['git_sha'] or 'n/a'} | last_seen={last_seen}"
            )

        return SkillResult(context_data="\n".join(lines))

    async def _fleet_summary(self) -> SkillResult:
        row = await asyncio.to_thread(
            postgres.execute_one,
            """
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE is_connected AND NOT is_revoked) as online,
                COUNT(*) FILTER (WHERE NOT is_connected AND NOT is_revoked) as offline,
                COUNT(DISTINCT sentinel_env) FILTER (WHERE NOT is_revoked) as envs
            FROM mesh_agents
            """,
        )
        if not row:
            return SkillResult(context_data="No mesh agents found.")
        summary = (
            f"**Fleet Summary**\n"
            f"Total agents: {row['total']} | "
            f"Online: {row['online']} 🟢 | "
            f"Offline: {row['offline']} 🔴 | "
            f"Environments: {row['envs']}"
        )
        return SkillResult(context_data=summary)

    async def _get(self, params: dict) -> SkillResult:
        agent_id = params.get("agent_id", "")
        row = await asyncio.to_thread(
            postgres.execute_one,
            "SELECT * FROM mesh_agents WHERE agent_id = %s AND is_revoked = FALSE",
            (agent_id,),
        )
        if not row:
            return SkillResult(context_data=f"Agent `{agent_id}` not found.")
        return SkillResult(
            context_data=f"Agent details:\n```\n{json.dumps(dict(row), default=str, indent=2)}\n```"
        )

    async def _health(self, params: dict) -> SkillResult:
        agent_id = params.get("agent_id", "")
        hb = await asyncio.to_thread(
            postgres.execute_one,
            """
            SELECT * FROM mesh_heartbeats
            WHERE agent_id = %s
            ORDER BY received_at DESC LIMIT 1
            """,
            (agent_id,),
        )
        if not hb:
            return SkillResult(context_data=f"No heartbeat data for agent `{agent_id}`.")
        return SkillResult(
            context_data=(
                f"**Latest Heartbeat** for `{agent_id}`\n"
                f"process_up={hb['process_up']} | cpu={hb['cpu_pct']}% | "
                f"mem={hb['mem_pct']}% | disk={hb['disk_pct']}% | "
                f"http_status={hb['http_status']} | received_at={hb['received_at']}"
            )
        )


class AgentManageSkill(BaseSkill):
    name = "agent_manage"
    description = (
        "Manage Sentinel Mesh Agent registrations: register new agents, revoke compromised "
        "agents, update agent metadata. Use when Anthony says 'register agent', 'add server "
        "to fleet', 'revoke agent', or 'remove [server] from mesh'. Requires CRITICAL approval. "
        "NOT for: viewing agents (use agent_registry) or running commands on agents (use agent_exec)."
    )
    trigger_intents = ["agent_manage"]
    approval_category = ApprovalCategory.CRITICAL

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "")
        agent_id = params.get("agent_id", "")

        if action == "provision":
            return await self._provision(params)
        elif action == "revoke":
            return await self._revoke(agent_id)
        elif action == "dispatch_patch":
            return SkillResult(
                context_data="Use PatchDispatchSkill for patch dispatch.",
                pending_action={
                    "action": "patch_dispatch",
                    "agent_id": agent_id,
                    "diff_text": params.get("diff_text", ""),
                },
            )
        else:
            return SkillResult(context_data="Supported actions: provision, revoke, dispatch_patch")

    async def _provision(self, params: dict) -> SkillResult:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "http://localhost:8000/api/v1/agents/provision",
                    json={
                        "app_name": params.get("app_name", "unnamed"),
                        "sentinel_env": params.get("sentinel_env", "staging"),
                        "hostname": params.get("hostname"),
                    },
                    timeout=10,
                )
                data = resp.json()
        except Exception as exc:
            return SkillResult(context_data=f"Provision failed: {exc}", is_error=True)

        return SkillResult(
            context_data=(
                f"**Agent Provisioned** ✅\n"
                f"agent_id: `{data.get('agent_id')}`\n"
                f"app_name: `{data.get('app_name')}`\n"
                f"env: `{data.get('sentinel_env')}`\n"
                f"ws_url: `{data.get('ws_url')}`\n\n"
                f"**Store this token securely (shown once):**\n"
                f"```\nAGENT_TOKEN={data.get('agent_token')}\n```"
            )
        )

    async def _revoke(self, agent_id: str) -> SkillResult:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://localhost:8000/api/v1/agents/{agent_id}/revoke",
                    timeout=10,
                )
                if resp.status_code == 404:
                    return SkillResult(context_data=f"Agent `{agent_id}` not found.")
        except Exception as exc:
            return SkillResult(context_data=f"Revoke failed: {exc}", is_error=True)

        return SkillResult(context_data=f"Agent `{agent_id}` has been revoked. ✅")
