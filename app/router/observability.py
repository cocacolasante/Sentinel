"""
Observability Router — /api/v1/observe

Endpoints:
  WS  /observe/stream   — WebSocket: real-time JSON event stream
  GET /observe/metrics  — current aggregate stats snapshot
  GET /observe/events   — last N events (HTTP, no streaming)
  GET /observe/health   — subscriber count + bus status
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from app.observability.event_bus import event_bus

router = APIRouter(prefix="/observe", tags=["observability"])


# ── WebSocket stream ──────────────────────────────────────────────────────────

@router.websocket("/stream")
async def event_stream(websocket: WebSocket):
    """
    Connect to receive a real-time stream of Brain lifecycle events.

    Each event is a JSON object with at minimum:
      { "event": str, "timestamp": str, "session_id"?: str }

    Event types: request_received | llm_called | skill_dispatched | response_delivered | error

    Usage (JavaScript):
      const ws = new WebSocket('wss://your-domain.com/api/v1/observe/stream');
      ws.onmessage = (e) => console.log(JSON.parse(e.data));

    Usage (Python):
      import websockets, asyncio, json
      async with websockets.connect('ws://localhost:8000/api/v1/observe/stream') as ws:
          async for msg in ws:
              print(json.loads(msg))
    """
    await websocket.accept()
    queue = event_bus.subscribe()
    logger.info("Observe WS connected | subscribers={}", event_bus.subscriber_count)

    # Send a welcome event so the client knows the connection is live
    await websocket.send_json({
        "event": "connected",
        "message": "Brain observability stream active",
        "subscriber_count": event_bus.subscriber_count,
    })

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # Send a heartbeat so proxies don't drop idle connections
                await websocket.send_json({"event": "heartbeat"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Observe WS error: {}", exc)
    finally:
        event_bus.unsubscribe(queue)
        logger.info("Observe WS disconnected | subscribers={}", event_bus.subscriber_count)


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@router.get("/metrics")
async def metrics():
    """
    Return aggregate Brain performance metrics.

    Includes: uptime, request count, error rate, latency percentiles (p50/p95/p99),
    intent breakdown, agent breakdown, model usage, token totals, and recent errors.
    """
    return event_bus.metrics.snapshot()


@router.get("/events")
async def recent_events(limit: int = Query(default=50, ge=1, le=100)):
    """Return the last N events from the in-memory buffer."""
    all_events = list(event_bus.metrics._recent)
    return {"events": all_events[-limit:], "total": len(all_events)}


@router.get("/health")
async def observe_health():
    """Return observability system health."""
    return {
        "status": "ok",
        "active_subscribers": event_bus.subscriber_count,
        "loop_bound": event_bus._loop is not None,
    }
