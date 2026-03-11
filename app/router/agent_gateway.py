"""
Sentinel Mesh Agent Gateway

REST endpoints (prefix /api/v1):
  POST /agents/provision                    — generate agent_id + HMAC secret
  GET  /agents/                             — list agents
  GET  /agents/{id}/health                  — latest heartbeat
  GET  /agents/{id}/patches                 — patch history
  POST /agents/{id}/revoke                  — revoke agent
  POST /agents/{id}/command                 — send CHAT_COMMAND to agent
  GET  /agents/{id}/responses/{corr_id}     — poll for CHAT_RESPONSE
  POST /agents/{id}/self-update             — push SELF_UPDATE to one agent
  POST /agents/update-all                   — broadcast SELF_UPDATE to all connected agents

WebSocket endpoint (no prefix):
  WS   /ws/agent/{agent_id}       — agent long-lived connection
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel

from app.config import get_settings
from app.db import postgres

settings = get_settings()

router = APIRouter(prefix="/agents", tags=["mesh-agents"])
ws_router = APIRouter(tags=["mesh-agents-ws"])


# ── Redis helpers ──────────────────────────────────────────────────────────────

def _get_redis():
    return aioredis.from_url(
        f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/0",
        decode_responses=True,
    )


# ── HMAC helpers ───────────────────────────────────────────────────────────────

def _hash_secret(raw_secret: str) -> str:
    """Store SHA-256(secret) in DB — never the raw secret."""
    return hashlib.sha256(raw_secret.encode()).hexdigest()


def _validate_message(msg: dict, secret_hash: str, max_drift: int) -> bool:
    """Verify timestamp freshness and HMAC signature."""
    try:
        ts = float(msg.get("ts", 0))
        if abs(time.time() - ts) > max_drift:
            return False
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        canonical = f"{ts}:{msg_type}:{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
        expected = hmac.new(
            secret_hash.encode(),
            canonical.encode(),
            "sha256",
        ).hexdigest()
        return hmac.compare_digest(expected, msg.get("sig", ""))
    except Exception:
        return False


# ── REST models ────────────────────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    app_name: str
    sentinel_env: str = "staging"
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    os_name: Optional[str] = None


class AgentCommandRequest(BaseModel):
    command: str                  # shell | read_logs | process_status | disk_usage | restart_app | read_file | list_files | write_file | env_info
    args: dict = {}
    timeout_secs: int = 30


class AgentChatRequest(BaseModel):
    message: str
    session_id: str = ""          # defaults to "agent:{agent_id}"


class AgentTaskRequest(BaseModel):
    description: str              # natural language task description
    autonomous: bool = True       # if False, requires step-by-step confirmation


class AgentSelfUpdateRequest(BaseModel):
    target_sha: str = ""          # specific commit SHA; empty = latest on branch
    branch: str = "main"
    force: bool = False           # update even if already on target SHA


# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.post("/provision")
async def provision_agent(req: ProvisionRequest):
    """Generate a new agent_id and HMAC secret. Secret returned only once."""
    raw_secret = secrets.token_hex(32)  # 256-bit
    secret_hash = _hash_secret(raw_secret)

    row = await asyncio.to_thread(
        postgres.execute_one,
        """
        INSERT INTO mesh_agents (app_name, sentinel_env, hostname, ip_address, os_name, hmac_secret)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING agent_id, app_name, sentinel_env, registered_at
        """,
        (req.app_name, req.sentinel_env, req.hostname, req.ip_address, req.os_name, secret_hash),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to provision agent")

    logger.info("Provisioned mesh agent | id={} app={}", row["agent_id"], row["app_name"])
    return {
        "agent_id": str(row["agent_id"]),
        "agent_token": raw_secret,  # returned once — never stored
        "app_name": row["app_name"],
        "sentinel_env": row["sentinel_env"],
        "registered_at": row["registered_at"].isoformat(),
        "ws_url": f"wss://{settings.domain}{settings.agent_ws_path}/{row['agent_id']}",
    }


@router.get("/")
async def list_agents(env: Optional[str] = None, connected: Optional[bool] = None):
    """List all mesh agents with optional filters."""
    conditions = ["is_revoked = FALSE"]
    params: list = []
    if env:
        params.append(env)
        conditions.append(f"sentinel_env = %s")
    if connected is not None:
        params.append(connected)
        conditions.append(f"is_connected = %s")
    where = " AND ".join(conditions)

    rows = await asyncio.to_thread(
        postgres.execute,
        f"""
        SELECT agent_id, app_name, hostname, ip_address, os_name,
               sentinel_env, agent_version, git_sha, is_connected, last_seen,
               registered_at
        FROM mesh_agents
        WHERE {where}
        ORDER BY registered_at DESC
        """,
        params or None,
    )
    return {"agents": [dict(r) for r in rows], "total": len(rows)}


@router.get("/{agent_id}/health")
async def get_agent_health(agent_id: str):
    """Return agent info + latest heartbeat."""
    agent = await asyncio.to_thread(
        postgres.execute_one,
        "SELECT * FROM mesh_agents WHERE agent_id = %s AND is_revoked = FALSE",
        (agent_id,),
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    heartbeat = await asyncio.to_thread(
        postgres.execute_one,
        """
        SELECT * FROM mesh_heartbeats
        WHERE agent_id = %s
        ORDER BY received_at DESC LIMIT 1
        """,
        (agent_id,),
    )
    return {
        "agent": dict(agent),
        "latest_heartbeat": dict(heartbeat) if heartbeat else None,
    }


@router.get("/{agent_id}/patches")
async def get_agent_patches(agent_id: str, limit: int = 20):
    """Return patch history for an agent."""
    rows = await asyncio.to_thread(
        postgres.execute,
        """
        SELECT id, patch_id, triggered_by, files_changed, status,
               approval_ts, approved_by, slack_ts, created_at, updated_at
        FROM mesh_patches
        WHERE agent_id = %s
        ORDER BY created_at DESC LIMIT %s
        """,
        (agent_id, limit),
    )
    return {"patches": [dict(r) for r in rows]}


@router.post("/{agent_id}/revoke")
async def revoke_agent(agent_id: str):
    """Revoke agent credentials and disconnect if active."""
    await asyncio.to_thread(
        postgres.execute,
        "UPDATE mesh_agents SET is_revoked = TRUE, is_connected = FALSE WHERE agent_id = %s",
        (agent_id,),
    )

    # Signal the WS connection to close
    redis = _get_redis()
    try:
        cmd = json.dumps({"type": "REVOKED", "payload": {}, "ts": time.time()})
        await redis.rpush(f"sentinel:agent:cmd:{agent_id}", cmd)
        await redis.expire(f"sentinel:agent:cmd:{agent_id}", 3600)
    finally:
        await redis.aclose()

    logger.info("Revoked mesh agent | id={}", agent_id)
    return {"status": "revoked", "agent_id": agent_id}


@router.post("/{agent_id}/command")
async def send_agent_command(agent_id: str, req: AgentCommandRequest):
    """Send a CHAT_COMMAND to a connected agent and return a correlation_id for polling."""
    agent = await asyncio.to_thread(
        postgres.execute_one,
        "SELECT app_name, hmac_secret, sentinel_env, is_connected FROM mesh_agents WHERE agent_id = %s AND is_revoked = FALSE",
        (agent_id,),
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent["is_connected"]:
        raise HTTPException(status_code=503, detail="Agent is offline")

    # Production restart requires pre-approval flag
    if req.command == "restart_app" and agent["sentinel_env"] == "production":
        if not req.args.get("approved"):
            raise HTTPException(
                status_code=403,
                detail="Production restart requires args.approved=true (set via Grafana confirm dialog)",
            )

    correlation_id = str(uuid.uuid4())
    ts = time.time()
    payload = {
        "correlation_id": correlation_id,
        "command": req.command,
        "args": req.args,
        "issued_by": "grafana",
        "issued_at": ts,
    }
    canonical = f"{ts}:CHAT_COMMAND:{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
    sig = hmac.new(
        agent["hmac_secret"].encode(),
        canonical.encode(),
        "sha256",
    ).hexdigest()
    cmd_msg = json.dumps({"type": "CHAT_COMMAND", "payload": payload, "ts": ts, "sig": sig})

    redis = _get_redis()
    try:
        await redis.rpush(f"sentinel:agent:cmd:{agent_id}", cmd_msg)
        await redis.expire(f"sentinel:agent:cmd:{agent_id}", 3600)
        await redis.set(
            f"sentinel:agent:chat_pending:{agent_id}:{correlation_id}",
            "pending",
            ex=req.timeout_secs + 10,
        )
    finally:
        await redis.aclose()

    # Audit + DB record (non-blocking)
    asyncio.create_task(_audit(agent_id, "chat_command_sent", "outbound", {"type": "CHAT_COMMAND", **payload}, True))
    asyncio.create_task(_insert_chat_command(agent_id, correlation_id, req.command, req.args, "grafana"))

    logger.info("CHAT_COMMAND dispatched | agent={} cmd={} corr={}", agent_id, req.command, correlation_id)
    return {"correlation_id": correlation_id, "status": "dispatched", "timeout_secs": req.timeout_secs}


@router.get("/{agent_id}/responses/{correlation_id}")
async def get_agent_response(agent_id: str, correlation_id: str):
    """Poll for a CHAT_RESPONSE from an agent. Returns status: ready|pending|expired."""
    resp_key = f"sentinel:agent:chat_response:{agent_id}:{correlation_id}"
    pend_key = f"sentinel:agent:chat_pending:{agent_id}:{correlation_id}"

    redis = _get_redis()
    try:
        raw = await redis.get(resp_key)
        if raw:
            data = json.loads(raw)
            await redis.delete(resp_key)  # read-once
            return {"status": "ready", "response": data}
        pending = await redis.get(pend_key)
        if pending:
            return {"status": "pending"}
    finally:
        await redis.aclose()

    raise HTTPException(status_code=404, detail="expired")


@router.post("/{agent_id}/chat")
async def agent_chat(agent_id: str, req: AgentChatRequest):
    """
    Chat with a mesh agent using the full Brain Dispatcher pipeline.

    The agent's live context (heartbeat metrics, patches, env) is injected
    into the session automatically so every Brain skill has full awareness
    of the remote agent. Responses use all 50+ registered skills, including
    AgentExecSkill which relays commands back to the agent's server as needed.
    """
    from app.brain.dispatcher import Dispatcher

    agent = await asyncio.to_thread(
        postgres.execute_one,
        "SELECT app_name, hostname, sentinel_env, git_sha, is_connected, "
        "       last_seen, last_heartbeat, agent_version "
        "FROM mesh_agents WHERE agent_id = %s AND is_revoked = FALSE",
        (agent_id,),
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Build rich context block prepended to every message
    hb = agent.get("last_heartbeat") or {}
    if isinstance(hb, str):
        try:
            import json as _json
            hb = _json.loads(hb)
        except Exception:
            hb = {}

    status = "ONLINE" if agent["is_connected"] else "OFFLINE"
    last_seen = str(agent.get("last_seen", "unknown"))
    context_block = (
        f"[AGENT CONTEXT]\n"
        f"agent_id: {agent_id}\n"
        f"app_name: {agent['app_name']} | env: {agent['sentinel_env']} | status: {status}\n"
        f"hostname: {agent.get('hostname', 'unknown')} | git_sha: {agent.get('git_sha', 'unknown')}\n"
        f"version: {agent.get('agent_version', 'unknown')} | last_seen: {last_seen}\n"
        f"process_up: {hb.get('process_up', 'unknown')} | "
        f"cpu: {hb.get('cpu_pct', '?')}% | mem: {hb.get('mem_pct', '?')}% | disk: {hb.get('disk_pct', '?')}%\n"
        f"http_status: {hb.get('http_status', 'unknown')}\n"
        f"[/AGENT CONTEXT]\n\n"
        f"{req.message}"
    )

    session_id = req.session_id.strip() or f"agent:{agent_id}"

    dispatch = Dispatcher()
    try:
        result = await dispatch.process(context_block, session_id)
    except Exception as exc:
        logger.error("Agent chat dispatch failed agent={}: {}", agent_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "reply": result.reply,
        "intent": result.intent,
        "agent": result.agent,
        "session_id": result.session_id,
        "agent_id": agent_id,
        "app_name": agent["app_name"],
    }


@router.post("/{agent_id}/task")
async def agent_task(agent_id: str, req: AgentTaskRequest):
    """
    Dispatch an autonomous multi-step task to be handled by the Brain.

    Brain uses its full skill set + AgentExecSkill to complete the task
    without requiring step-by-step user interaction. Task progress is
    reported to Slack and can be polled via the task board.
    """
    agent = await asyncio.to_thread(
        postgres.execute_one,
        "SELECT app_name, sentinel_env FROM mesh_agents WHERE agent_id = %s AND is_revoked = FALSE",
        (agent_id,),
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    from app.worker.agent_tasks import run_agent_task
    task_data = run_agent_task.delay(agent_id, agent["app_name"], agent["sentinel_env"], req.description)

    logger.info("Autonomous task queued | agent={} app={}", agent_id, agent["app_name"])
    return {
        "status": "queued",
        "celery_task_id": task_data.id,
        "agent_id": agent_id,
        "app_name": agent["app_name"],
        "description": req.description,
    }


@router.post("/update-all")
async def broadcast_self_update(req: AgentSelfUpdateRequest):
    """
    Push a SELF_UPDATE command to every currently-connected mesh agent.
    Called automatically by deploy.sh when sentinel-agent/ code changes.
    """
    from app.worker.agent_tasks import broadcast_agent_updates
    task = broadcast_agent_updates.delay(req.target_sha, req.branch, req.force)
    logger.info("Agent self-update broadcast queued | sha={} branch={}", req.target_sha or "latest", req.branch)
    return {"status": "queued", "celery_task_id": task.id, "target_sha": req.target_sha, "branch": req.branch}


@router.post("/{agent_id}/self-update")
async def self_update_agent(agent_id: str, req: AgentSelfUpdateRequest):
    """Push a SELF_UPDATE command to a single agent."""
    agent = await asyncio.to_thread(
        postgres.execute_one,
        "SELECT app_name, hmac_secret, sentinel_env, is_connected FROM mesh_agents "
        "WHERE agent_id = %s AND is_revoked = FALSE",
        (agent_id,),
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent["is_connected"]:
        raise HTTPException(status_code=503, detail="Agent is offline — cannot push update")

    correlation_id = str(uuid.uuid4())
    ts = time.time()
    payload = {
        "correlation_id": correlation_id,
        "target_sha": req.target_sha,
        "branch": req.branch,
        "force": req.force,
    }
    canonical = f"{ts}:SELF_UPDATE:{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
    sig = hmac.new(agent["hmac_secret"].encode(), canonical.encode(), "sha256").hexdigest()
    cmd_msg = json.dumps({"type": "SELF_UPDATE", "payload": payload, "ts": ts, "sig": sig})

    redis = _get_redis()
    try:
        await redis.rpush(f"sentinel:agent:cmd:{agent_id}", cmd_msg)
        await redis.expire(f"sentinel:agent:cmd:{agent_id}", 3600)
    finally:
        await redis.aclose()

    asyncio.create_task(_audit(agent_id, "self_update_sent", "outbound", {"type": "SELF_UPDATE", **payload}, True))
    logger.info("SELF_UPDATE dispatched | agent={} sha={}", agent_id, req.target_sha or "latest")
    return {
        "status": "dispatched",
        "agent_id": agent_id,
        "app_name": agent["app_name"],
        "correlation_id": correlation_id,
        "target_sha": req.target_sha,
    }


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@ws_router.websocket("/ws/agent/{agent_id}")
async def agent_ws_endpoint(websocket: WebSocket, agent_id: str):
    """Long-lived WebSocket connection for a mesh agent."""
    agent = await asyncio.to_thread(
        postgres.execute_one,
        "SELECT * FROM mesh_agents WHERE agent_id = %s",
        (agent_id,),
    )

    if not agent or agent["is_revoked"]:
        await websocket.close(code=4001, reason="Unknown or revoked agent")
        return

    await websocket.accept()
    logger.info("Agent connected | id={} app={}", agent_id, agent["app_name"])

    await asyncio.to_thread(
        postgres.execute,
        "UPDATE mesh_agents SET is_connected = TRUE, last_seen = NOW() WHERE agent_id = %s",
        (agent_id,),
    )

    secret_hash = agent["hmac_secret"]
    redis = _get_redis()

    async def _recv_loop():
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Agent {} sent invalid JSON", agent_id)
                    continue

                sig_valid = _validate_message(msg, secret_hash, settings.agent_hmac_ts_drift_max)
                asyncio.create_task(_audit(agent_id, msg.get("type", "unknown"), "inbound", msg, sig_valid))

                if not sig_valid:
                    logger.warning("Agent {} HMAC validation failed", agent_id)
                    continue

                # Fast-path: store CHAT_RESPONSE directly in Redis (avoids 60s Celery lag)
                if msg.get("type") == "CHAT_RESPONSE":
                    inner = msg.get("payload", {})
                    corr_id = inner.get("correlation_id", "")
                    if corr_id:
                        resp_key = f"sentinel:agent:chat_response:{agent_id}:{corr_id}"
                        pend_key = f"sentinel:agent:chat_pending:{agent_id}:{corr_id}"
                        await redis.set(resp_key, json.dumps(inner), ex=300)
                        await redis.delete(pend_key)
                        asyncio.create_task(_update_chat_command_row(agent_id, corr_id, inner))

                stream_entry = json.dumps({
                    "agent_id": agent_id,
                    "app_name": agent["app_name"],
                    "sentinel_env": agent["sentinel_env"],
                    **msg,
                })
                await redis.rpush(settings.agent_stream_key, stream_entry)
                await redis.expire(settings.agent_stream_key, 86400 * 7)

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("Agent recv error | id={} err={}", agent_id, exc)

    async def _send_loop():
        cmd_key = f"sentinel:agent:cmd:{agent_id}"
        try:
            while True:
                result = await redis.blpop(cmd_key, timeout=30)
                if result:
                    _, cmd_json = result
                    await websocket.send_text(cmd_json)
                    asyncio.create_task(
                        _audit(agent_id, "cmd_sent", "outbound", json.loads(cmd_json), None)
                    )
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("Agent send error | id={} err={}", agent_id, exc)

    try:
        await asyncio.gather(_recv_loop(), _send_loop())
    finally:
        await redis.aclose()
        await asyncio.to_thread(
            postgres.execute,
            "UPDATE mesh_agents SET is_connected = FALSE WHERE agent_id = %s",
            (agent_id,),
        )
        logger.info("Agent disconnected | id={}", agent_id)


async def _audit(
    agent_id: str,
    event_type: str,
    direction: str,
    msg: dict,
    sig_valid: bool | None,
):
    """Write an entry to mesh_audit_log (non-fatal)."""
    try:
        drift_ms = None
        if "ts" in msg:
            drift_ms = abs(time.time() - float(msg["ts"])) * 1000
        await asyncio.to_thread(
            postgres.execute,
            """
            INSERT INTO mesh_audit_log
                (agent_id, event_type, direction, message_type, payload_summary, sig_valid, ts_drift_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                agent_id,
                event_type,
                direction,
                msg.get("type"),
                json.dumps({"type": msg.get("type"), "ts": msg.get("ts")}),
                sig_valid,
                drift_ms,
            ),
        )
    except Exception as exc:
        logger.debug("Audit log write failed (non-fatal): {}", exc)


async def _insert_chat_command(agent_id: str, correlation_id: str, command: str, args: dict, issued_by: str):
    """Insert initial row into mesh_chat_commands (non-fatal)."""
    try:
        await asyncio.to_thread(
            postgres.execute,
            """
            INSERT INTO mesh_chat_commands (agent_id, correlation_id, command, args, issued_by)
            VALUES (%s, %s::uuid, %s, %s, %s)
            ON CONFLICT (correlation_id) DO NOTHING
            """,
            (agent_id, correlation_id, command, json.dumps(args), issued_by),
        )
    except Exception as exc:
        logger.debug("mesh_chat_commands insert failed (non-fatal): {}", exc)


async def _update_chat_command_row(agent_id: str, correlation_id: str, payload: dict):
    """Update mesh_chat_commands with response data (non-fatal)."""
    try:
        await asyncio.to_thread(
            postgres.execute,
            """
            UPDATE mesh_chat_commands
            SET success = %s, elapsed_ms = %s, error = %s, responded_at = NOW()
            WHERE correlation_id = %s::uuid
            """,
            (
                payload.get("success"),
                payload.get("elapsed_ms"),
                payload.get("error"),
                correlation_id,
            ),
        )
    except Exception as exc:
        logger.debug("mesh_chat_commands update failed (non-fatal): {}", exc)
