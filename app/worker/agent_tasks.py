"""
Celery tasks for Sentinel Mesh Agent management.

Tasks:
  check_agent_heartbeats   — every 2min: mark offline agents + Slack alert
  process_agent_stream     — every 1min: consume Redis stream, route by message type
  purge_old_heartbeats     — daily 03:00 UTC: DELETE heartbeats older than 7 days
  index_agent_codebase     — on REGISTER: chunk+embed file tree into Qdrant
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import redis as sync_redis
from celery import shared_task
from loguru import logger

from app.config import get_settings
from app.integrations.slack_notifier import post_alert_sync

settings = get_settings()


def _get_db_sync():
    """Synchronous Postgres connection for Celery tasks."""
    import psycopg2
    return psycopg2.connect(settings.postgres_dsn)


def _get_redis_sync():
    return sync_redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        db=0,
        decode_responses=True,
    )


# ── Task: heartbeat monitor ────────────────────────────────────────────────────

@shared_task(name="app.worker.agent_tasks.check_agent_heartbeats")
def check_agent_heartbeats():
    """Mark agents as offline if last_seen exceeds timeout threshold."""
    timeout_sec = settings.agent_heartbeat_timeout
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_sec)

    conn = _get_db_sync()
    try:
        with conn.cursor() as cur:
            # Find connected agents that haven't been seen recently
            cur.execute(
                """
                SELECT agent_id, app_name, hostname, sentinel_env
                FROM mesh_agents
                WHERE is_connected = TRUE AND is_revoked = FALSE
                  AND last_seen < %s
                """,
                (cutoff,),
            )
            stale = cur.fetchall()

            if stale:
                agent_ids = [row[0] for row in stale]
                cur.execute(
                    "UPDATE mesh_agents SET is_connected = FALSE WHERE agent_id = ANY(%s)",
                    (agent_ids,),
                )
                conn.commit()

                for agent_id, app_name, hostname, sentinel_env in stale:
                    msg = (
                        f"⚠️ *Mesh Agent Offline* | `{app_name}` (`{hostname}`)\n"
                        f"env={sentinel_env} | agent_id=`{agent_id}`\n"
                        f"Last seen > {timeout_sec}s ago"
                    )
                    try:
                        post_alert_sync(msg, settings.slack_agents_channel)
                    except Exception as e:
                        logger.warning("Slack alert failed for offline agent: {}", e)
                    logger.warning("Agent marked offline | id={} app={}", agent_id, app_name)
    finally:
        conn.close()


# ── Task: stream consumer ──────────────────────────────────────────────────────

@shared_task(name="app.worker.agent_tasks.process_agent_stream")
def process_agent_stream():
    """Consume up to 100 messages from the agent stream and route them."""
    redis = _get_redis_sync()
    stream_key = settings.agent_stream_key
    cursor_key = f"{stream_key}:last_id"

    # Read batch
    batch = redis.lrange(stream_key, 0, 99)
    if not batch:
        return

    # Remove processed items
    redis.ltrim(stream_key, len(batch), -1)

    conn = _get_db_sync()
    try:
        for raw in batch:
            try:
                msg = json.loads(raw)
                _route_stream_message(msg, conn, redis)
            except Exception as exc:
                logger.error("Stream message routing failed: {} | raw={}", exc, raw[:200])
        conn.commit()
    finally:
        conn.close()
    redis.close()


def _route_stream_message(msg: dict, conn, redis):
    """Route a single stream message by type."""
    agent_id = msg.get("agent_id")
    msg_type = msg.get("type", "")
    payload = msg.get("payload", {})

    with conn.cursor() as cur:
        if msg_type == "REGISTER":
            _handle_register(cur, agent_id, msg, redis)
        elif msg_type == "HEARTBEAT":
            _handle_heartbeat(cur, agent_id, payload)
        elif msg_type == "LOG_ERROR":
            _handle_log_error(agent_id, payload)
        elif msg_type == "PATCH_RESULT":
            _handle_patch_result(cur, agent_id, payload)
        elif msg_type == "GIT_UPDATE":
            cur.execute(
                "UPDATE mesh_agents SET git_sha = %s WHERE agent_id = %s",
                (payload.get("sha"), agent_id),
            )
        elif msg_type == "HTTP_STATUS":
            # Update latest heartbeat
            cur.execute(
                """
                INSERT INTO mesh_heartbeats (agent_id, http_status, http_latency_ms)
                VALUES (%s, %s, %s)
                """,
                (agent_id, payload.get("status_code"), payload.get("latency_ms")),
            )
        elif msg_type == "RESOURCE_ALERT":
            _handle_resource_alert(agent_id, msg, payload)


def _handle_register(cur, agent_id: str, msg: dict, redis):
    """Upsert agent on REGISTER; send REGISTER_ACK."""
    payload = msg.get("payload", {})
    cur.execute(
        """
        UPDATE mesh_agents
        SET app_name = COALESCE(%s, app_name),
            hostname = COALESCE(%s, hostname),
            ip_address = COALESCE(%s, ip_address),
            os_name = COALESCE(%s, os_name),
            agent_version = COALESCE(%s, agent_version),
            git_sha = COALESCE(%s, git_sha),
            is_connected = TRUE,
            last_seen = NOW(),
            updated_at = NOW()
        WHERE agent_id = %s
        """,
        (
            payload.get("app_name"),
            payload.get("hostname"),
            payload.get("ip_address"),
            payload.get("os_name"),
            payload.get("agent_version"),
            payload.get("git_sha"),
            agent_id,
        ),
    )
    # Send ACK
    ack = json.dumps({
        "type": "REGISTER_ACK",
        "payload": {"status": "ok", "agent_id": agent_id},
        "ts": time.time(),
    })
    redis.rpush(f"sentinel:agent:cmd:{agent_id}", ack)
    redis.expire(f"sentinel:agent:cmd:{agent_id}", 3600)
    logger.info("Agent registered | id={}", agent_id)


def _handle_heartbeat(cur, agent_id: str, payload: dict):
    """Insert heartbeat row and update agent last_seen."""
    cur.execute(
        """
        INSERT INTO mesh_heartbeats
            (agent_id, process_up, cpu_pct, mem_pct, disk_pct, git_sha, http_status, http_latency_ms, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            agent_id,
            payload.get("process_up"),
            payload.get("cpu_pct"),
            payload.get("mem_pct"),
            payload.get("disk_pct"),
            payload.get("git_sha"),
            payload.get("http_status"),
            payload.get("http_latency_ms"),
            json.dumps(payload),
        ),
    )
    cur.execute(
        "UPDATE mesh_agents SET last_seen = NOW(), last_heartbeat = %s WHERE agent_id = %s",
        (json.dumps(payload), agent_id),
    )


def _handle_log_error(agent_id: str, payload: dict):
    """Trigger remote log analysis → patch dispatch via Celery."""
    try:
        from app.worker.tasks import execute_board_task  # avoid circular
        logger.info("LOG_ERROR from agent {} — queuing analysis", agent_id)
        # Store event for async processing
        redis = _get_redis_sync()
        redis.rpush(
            f"sentinel:agent:log_errors:{agent_id}",
            json.dumps({"agent_id": agent_id, "payload": payload, "ts": time.time()}),
        )
        redis.expire(f"sentinel:agent:log_errors:{agent_id}", 3600)
        redis.close()
    except Exception as exc:
        logger.error("Log error handler failed: {}", exc)


def _handle_patch_result(cur, agent_id: str, payload: dict):
    """Update patch status based on agent report."""
    patch_id = payload.get("patch_id")
    success = payload.get("success", False)
    status = "applied" if success else "failed"
    if payload.get("action") == "rolled_back":
        status = "rolled_back"

    if patch_id:
        cur.execute(
            """
            UPDATE mesh_patches
            SET status = %s, result_logs = %s, updated_at = NOW()
            WHERE patch_id = %s
            """,
            (status, payload.get("logs", ""), patch_id),
        )

    msg = (
        f"{'✅' if success else '❌'} *Patch {status}* | agent=`{agent_id}`\n"
        f"patch_id=`{patch_id}`"
        + (f"\n```{payload.get('logs', '')}```" if payload.get("logs") else "")
    )
    try:
        post_alert_sync(msg, settings.slack_agents_channel)
    except Exception as e:
        logger.warning("Slack patch result notification failed: {}", e)


def _handle_resource_alert(agent_id: str, msg: dict, payload: dict):
    """Post Slack alert for resource threshold breach."""
    metric = payload.get("metric", "unknown")
    value = payload.get("value", 0)
    threshold = payload.get("threshold", 0)
    app_name = msg.get("app_name", agent_id)

    alert = (
        f"🔥 *Resource Alert* | `{app_name}`\n"
        f"{metric}={value:.1f}% (threshold={threshold:.0f}%)\n"
        f"agent_id=`{agent_id}`"
    )
    try:
        post_alert_sync(alert, settings.slack_agents_channel)
    except Exception as e:
        logger.warning("Slack resource alert failed: {}", e)


# ── Task: heartbeat purge ──────────────────────────────────────────────────────

@shared_task(name="app.worker.agent_tasks.purge_old_heartbeats")
def purge_old_heartbeats():
    """Delete heartbeat rows older than 7 days."""
    conn = _get_db_sync()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mesh_heartbeats WHERE received_at < NOW() - INTERVAL '7 days'"
            )
            deleted = cur.rowcount
            conn.commit()
        logger.info("Purged {} old mesh heartbeat rows", deleted)
    finally:
        conn.close()


# ── Task: codebase indexer ─────────────────────────────────────────────────────

@shared_task(name="app.worker.agent_tasks.index_agent_codebase")
def index_agent_codebase(agent_id: str, file_tree: dict):
    """Chunk and embed agent codebase into Qdrant for cross-agent search."""
    try:
        from app.memory.qdrant_client import QdrantMemory
        import asyncio

        namespace = f"agent:{agent_id}:codebase"
        files = file_tree.get("files", [])
        logger.info("Indexing {} files for agent {}", len(files), agent_id)

        qm = QdrantMemory(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            collection=settings.qdrant_collection,
        )

        async def _index():
            await qm.init_collection()
            for f in files[:50]:  # cap at 50 files per agent
                path = f.get("path", "")
                content = f.get("content", "")
                if not content:
                    continue
                # Store as memory entry with agent namespace metadata
                await qm.store(
                    content=f"[{path}]\n{content[:2000]}",
                    metadata={
                        "agent_id": agent_id,
                        "namespace": namespace,
                        "file_path": path,
                        "type": "agent_codebase",
                    },
                    session_id=namespace,
                )

        asyncio.run(_index())
        logger.info("Indexed codebase for agent {}", agent_id)
    except Exception as exc:
        logger.error("Codebase indexing failed for agent {}: {}", agent_id, exc)
