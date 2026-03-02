"""
Custom Prometheus metrics for the AI Brain.

These complement the auto-generated HTTP metrics from
prometheus-fastapi-instrumentator with brain-level semantics.

Exposed at GET /metrics alongside the instrumentator metrics.
Prometheus scrapes brain:8000/metrics every 15s (configured in prometheus.yml).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Request / response ────────────────────────────────────────────────────────

REQUESTS_TOTAL = Counter(
    "brain_requests_total",
    "Total chat requests processed by the brain",
    ["intent", "agent", "success"],
)

RESPONSE_LATENCY = Histogram(
    "brain_response_latency_seconds",
    "End-to-end latency from request received to reply delivered",
    ["intent", "agent"],
    buckets=[0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, float("inf")],
)

# ── LLM ───────────────────────────────────────────────────────────────────────

LLM_TOKENS = Counter(
    "brain_llm_tokens_total",
    "Total LLM tokens consumed",
    ["model", "direction"],  # direction: input | output
)

LLM_LATENCY = Histogram(
    "brain_llm_latency_seconds",
    "LLM API round-trip latency",
    ["model", "agent"],
    buckets=[0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, float("inf")],
)

LLM_REQUESTS = Counter(
    "brain_llm_requests_total",
    "Total LLM requests routed",
    ["model", "agent", "source", "intent"],
)

LLM_COST_USD = Counter(
    "brain_llm_cost_usd_total",
    "Cumulative LLM cost in USD",
    ["model", "agent"],
)

# ── Skills ────────────────────────────────────────────────────────────────────

SKILL_LATENCY = Histogram(
    "brain_skill_duration_seconds",
    "Skill execution duration",
    ["skill"],
    buckets=[0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, float("inf")],
)

# ── Cost tracking ─────────────────────────────────────────────────────────────

COST_DAILY_USD = Gauge(
    "brain_cost_usd_daily",
    "Total LLM cost accumulated today in USD (resets at midnight UTC)",
)

COST_CEILING_USD = Gauge(
    "brain_cost_ceiling_usd",
    "Configured daily LLM cost ceiling in USD",
)

BUDGET_EXCEEDED_TOTAL = Counter(
    "brain_budget_exceeded_total",
    "Number of LLM calls blocked due to budget ceiling",
)

RATE_LIMITED_TOTAL = Counter(
    "brain_rate_limited_total",
    "Number of requests blocked by per-session rate limiter",
    ["window"],  # window: minute | hour
)

ACTIVE_SESSIONS = Gauge(
    "brain_active_sessions",
    "Number of sessions with activity in the last 4 hours (Redis hot memory)",
)
