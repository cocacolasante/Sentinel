"""
EventBus — async pub/sub for Brain lifecycle events.

All four telemetry points publish here:
  request_received  → fired in LoggingHook PRE_PROCESS
  llm_called        → fired in LLMRouter.route() (thread pool — uses publish_sync)
  skill_dispatched  → fired in Dispatcher after skill.execute()
  response_delivered → fired in LoggingHook POST_PROCESS

Subscribers are WebSocket connections in the observability router.
MetricsStore keeps in-memory aggregates for the /metrics endpoint.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timezone


# ── Metrics store ─────────────────────────────────────────────────────────────

class MetricsStore:
    """In-memory rolling metrics. Not persisted — resets on restart."""

    def __init__(self) -> None:
        self._start         = time.time()
        self._total_req     = 0
        self._total_errors  = 0
        self._latencies_ms: list[float]   = []   # last 200 completed requests
        self._intents:      dict[str, int] = defaultdict(int)
        self._agents:       dict[str, int] = defaultdict(int)
        self._models:       dict[str, int] = defaultdict(int)
        self._token_totals: dict[str, int] = defaultdict(int)
        self._recent:       deque[dict]    = deque(maxlen=100)
        self._errors:       deque[dict]    = deque(maxlen=50)

    def record(self, event: dict) -> None:
        etype = event.get("event", "")
        self._recent.append(event)

        if etype == "request_received":
            self._total_req += 1

        elif etype == "llm_called":
            model = event.get("model", "unknown")
            self._models[model] = self._models.get(model, 0) + 1
            self._token_totals["input"]  = self._token_totals.get("input", 0)  + event.get("input_tokens", 0)
            self._token_totals["output"] = self._token_totals.get("output", 0) + event.get("output_tokens", 0)

        elif etype == "response_delivered":
            latency = event.get("latency_ms", 0.0)
            self._latencies_ms.append(latency)
            if len(self._latencies_ms) > 200:
                self._latencies_ms.pop(0)
            intent = event.get("intent", "unknown")
            agent  = event.get("agent",  "unknown")
            self._intents[intent] = self._intents.get(intent, 0) + 1
            self._agents[agent]   = self._agents.get(agent, 0)   + 1
            if event.get("error"):
                self._total_errors += 1
                self._errors.append(event)

        elif etype == "error":
            self._total_errors += 1
            self._errors.append(event)

        self._record_prom(event)

    def _record_prom(self, event: dict) -> None:
        """Update Prometheus counters/histograms. Silent no-op if prometheus_client unavailable."""
        try:
            from app.observability.prometheus_metrics import (
                REQUESTS_TOTAL, RESPONSE_LATENCY, LLM_TOKENS, LLM_LATENCY, SKILL_LATENCY,
            )
            etype = event.get("event", "")

            if etype == "llm_called":
                model = event.get("model", "unknown")
                LLM_TOKENS.labels(model=model, direction="input").inc(event.get("input_tokens", 0))
                LLM_TOKENS.labels(model=model, direction="output").inc(event.get("output_tokens", 0))
                LLM_LATENCY.labels(model=model).observe(event.get("latency_ms", 0) / 1000)

            elif etype == "skill_dispatched":
                skill = event.get("skill", "unknown")
                SKILL_LATENCY.labels(skill=skill).observe(event.get("latency_ms", 0) / 1000)

            elif etype == "response_delivered":
                intent  = event.get("intent", "unknown")
                agent   = event.get("agent",  "unknown")
                success = "false" if event.get("error") else "true"
                REQUESTS_TOTAL.labels(intent=intent, agent=agent, success=success).inc()
                RESPONSE_LATENCY.labels(intent=intent, agent=agent).observe(
                    event.get("latency_ms", 0) / 1000
                )
        except Exception:
            pass  # never let Prometheus errors break the event bus

    def snapshot(self) -> dict:
        lat = self._latencies_ms
        sorted_lat = sorted(lat) if lat else []
        def percentile(pct: float) -> float | None:
            if len(sorted_lat) < 10:
                return None
            idx = int(len(sorted_lat) * pct)
            return round(sorted_lat[min(idx, len(sorted_lat) - 1)], 1)

        return {
            "uptime_seconds":  round(time.time() - self._start),
            "total_requests":  self._total_req,
            "total_errors":    self._total_errors,
            "error_rate":      round(self._total_errors / self._total_req, 4) if self._total_req else 0.0,
            "latency_ms": {
                "avg": round(sum(lat) / len(lat), 1) if lat else None,
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "p99": percentile(0.99),
            },
            "intents":         dict(self._intents),
            "agents":          dict(self._agents),
            "models":          dict(self._models),
            "tokens": {
                "total_input":  self._token_totals.get("input", 0),
                "total_output": self._token_totals.get("output", 0),
            },
            "recent_errors":   list(self._errors)[-10:],
        }


# ── Event bus ─────────────────────────────────────────────────────────────────

class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self.metrics = MetricsStore()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called during app startup to bind the running event loop."""
        self._loop = loop

    def _stamp(self, event: dict) -> dict:
        event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        return event

    async def publish(self, event: dict) -> None:
        """Async publish — call from coroutines."""
        event = self._stamp(event)
        self.metrics.record(event)
        slow: list[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                slow.append(q)
        for q in slow:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish_sync(self, event: dict) -> None:
        """
        Thread-safe publish for use in worker threads (LLM router runs in a thread pool).
        Schedules the coroutine on the main event loop without blocking the caller.
        """
        event = self._stamp(event)
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(event), self._loop)
        else:
            # Fallback: just record metrics synchronously if no loop yet
            self.metrics.record(event)

    def subscribe(self) -> asyncio.Queue:
        """Return a new Queue that receives all future events."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# ── Singleton ─────────────────────────────────────────────────────────────────

event_bus = EventBus()
