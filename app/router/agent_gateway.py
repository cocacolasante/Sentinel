"""
Sentinel Mesh Agent Gateway

REST endpoints (prefix /api/v1):
  POST /agents/provision          — generate agent_id + HMAC secret
  GET  /agents/                   — list agents
  GET  /agents/{id}/health        — latest heartbeat
  GET  /agents/{id}/patches       — patch history
  POST /agents/{id}/revoke        — revoke agent

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
