"""
AI Brain — FastAPI entry point.

Startup sequence:
  1. Loguru configured (stdout + JSON file)
  2. Sentry initialised (if SENTRY_DSN is set)
  3. Event bus loop reference bound (for thread-safe publish_sync)
  4. PostgreSQL schema initialised (idempotent)
  5. Qdrant collection initialised
  6. Slack Socket Mode launched as background task
  7. REST + WebSocket endpoints available at /api/v1/

Routes:
  GET  /                              -- health probe
  GET  /api/v1/health                 -- detailed health (Redis + Postgres)
  POST /api/v1/chat                   -- main chat endpoint (intent-routed)
  DELETE /api/v1/chat/{id}            -- clear session history
  POST /api/v1/telos/reload           -- reload TELOS personal context
  GET  /api/v1/agents                 -- list registered agent personas
  GET  /api/v1/integrations/status    -- integration health check
  POST /api/v1/feedback/rate          -- rate an interaction (1-10)
  POST /api/v1/feedback/thumbs        -- thumbs up/down
  GET  /api/v1/feedback/summary       -- aggregate rating stats
  WS   /api/v1/observe/stream         -- real-time event stream (WebSocket)
  GET  /api/v1/observe/metrics        -- aggregate performance metrics
  GET  /api/v1/observe/events         -- recent event buffer
  GET  /api/v1/costs                  -- daily LLM spend and remaining budget
  GET  /api/v1/approval/level         -- current global approval level
  POST /api/v1/approval/level         -- set approval level {level: 1|2|3}
  GET  /api/v1/approval/pending       -- write tasks awaiting approval
  GET  /api/v1/approval/history       -- recent completed/failed write tasks
  POST /api/v1/approval/approve/{id}  -- approve a pending write task
  POST /api/v1/approval/cancel/{id}   -- cancel a pending write task
  POST /api/v1/sentry/webhook         -- Sentry alert webhook → Brain task (severity-gated)
  GET  /api/v1/sentry/issues          -- recent Sentry issues tracked in DB

Scheduled jobs (Celery Beat — separate celery-beat container):
  Weekly   Sun 09:00 UTC  -- agent quality evals + Slack scorecard
  Nightly  02:00 UTC      -- integration reliability checks
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.config import get_settings

settings = get_settings()


def _init_loguru() -> None:
    """Configure Loguru before anything else logs."""
    from app.observability.loguru_setup import configure
    configure(
        log_dir=settings.log_dir,
        level=settings.log_level,
    )


def _init_sentry() -> None:
    """Initialise Sentry if DSN is configured. Safe no-op if not."""
    if not settings.sentry_dsn:
        logger.info("Sentry DSN not set — error tracking disabled")
        return
    try:
        import logging as _logging
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            traces_sample_rate=0.2,
            profiles_sample_rate=0.1,
            max_breadcrumbs=40,          # keep breadcrumb buffer small
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                AsyncioIntegration(),
                LoggingIntegration(
                    level=_logging.WARNING,  # WARNING+ as breadcrumbs (not every INFO line)
                    event_level=_logging.ERROR,
                ),
            ],
            before_send=_sentry_before_send,
            before_breadcrumb=_sentry_before_breadcrumb,
        )
        logger.info("Sentry initialised | env={}", settings.environment)
    except ImportError:
        logger.warning("sentry-sdk not installed — run: pip install sentry-sdk[fastapi]")
    except Exception as exc:
        logger.error("Sentry init failed (non-fatal): {}", exc)


# Transient errors that are expected and self-recovering — never worth an alert.
_SENTRY_IGNORED_EXCEPTIONS = {
    "WebSocketDisconnect",
    "CancelledError",
    "ClientDisconnect",
    # Slack SDK race condition: monitor tears down stale session while a write
    # is in flight.  The SDK reconnects automatically — no action needed.
    "ClientConnectionResetError",
    "ConnectionResetError",
    "ServerConnectionError",
}


def _sentry_before_send(event, hint):
    """Drop known-harmless transient exceptions before they reach Sentry."""
    exc_info = hint.get("exc_info")
    if exc_info and exc_info[0].__name__ in _SENTRY_IGNORED_EXCEPTIONS:
        return None
    return event


def _sentry_before_breadcrumb(crumb, hint):
    """Drop high-frequency / low-signal breadcrumbs to keep reports readable."""
    category = crumb.get("category", "")
    # Drop all raw HTTP breadcrumbs — they flood every report with Slack API
    # calls, Qdrant pings, and Prometheus scrapes that add no debugging value.
    if category in ("httplib", "http"):
        return None
    return crumb


# ── Loguru and Sentry must init before any other imports that log ─────────────
_init_loguru()
_init_sentry()

from app.router import chat                                               # noqa: E402
from app.router.integrations   import router as integrations_router      # noqa: E402
from app.router.feedback       import router as feedback_router          # noqa: E402
from app.router.observability  import router as observe_router           # noqa: E402
from app.router.costs          import router as costs_router             # noqa: E402
from app.router.tasks_control  import router as tasks_control_router     # noqa: E402
from app.router.approval       import router as approval_router           # noqa: E402
from app.router.whatsapp        import router as whatsapp_router          # noqa: E402
from app.router.sentry_webhook  import router as sentry_webhook_router    # noqa: E402
from app.router.task_board      import router as task_board_router         # noqa: E402
from app.router.milestones      import router as milestones_router          # noqa: E402
from app.services.error_api    import router as error_api_router            # noqa: E402
from app.router.slack          import start_socket_mode                  # noqa: E402
from app.observability.event_bus import event_bus                    # noqa: E402
from prometheus_fastapi_instrumentator import Instrumentator          # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Brain starting | environment={}", settings.environment)

    # Bind the running event loop to the event bus so publish_sync() works
    # from worker threads (LLM router runs via asyncio.to_thread)
    event_bus.set_loop(asyncio.get_event_loop())
    logger.info("Event bus loop bound | observe_ws=ws://localhost:8000/api/v1/observe/stream")

    # Initialise PostgreSQL schema (creates tables if they don't exist)
    try:
        from app.db import postgres
        await asyncio.to_thread(postgres.init_schema)
        logger.info("PostgreSQL ready")
    except Exception as exc:
        logger.error("PostgreSQL init failed — check POSTGRES_* env vars: {}", exc)

    # Initialise Qdrant collection
    try:
        from app.memory.qdrant_client import QdrantMemory
        qm = QdrantMemory(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            collection=settings.qdrant_collection,
        )
        await qm.init_collection()
        logger.info("Qdrant ready | collection={}", settings.qdrant_collection)
    except Exception as exc:
        logger.error("Qdrant init failed (non-fatal): {}", exc)

    # Launch Slack Socket Mode in the background (non-blocking)
    asyncio.create_task(start_socket_mode())

    # Eval scheduling is handled by the Celery Beat container.
    # APScheduler is not started here — see app/worker/celery_app.py.

    logger.info("Brain ready")
    yield

    logger.info("Brain shutting down")


app = FastAPI(
    title="AI Brain",
    description="Personalized AI Assistant — CSuite Code",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

from app.services.error_middleware import ErrorCollectionMiddleware  # noqa: E402
app.add_middleware(ErrorCollectionMiddleware)

# Prometheus HTTP instrumentation — exposes /metrics for Prometheus scraping.
# Scraped internally by Prometheus (brain:8000/metrics); not proxied publicly.
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics", "/", "/api/v1/health"],
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# CORS — allow Grafana (localhost:3000) to call the Brain API in local dev.
# In production both are on the same Nginx domain so CORS is not needed.
if settings.environment != "production":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

app.include_router(chat.router,          prefix="/api/v1", tags=["chat"])
app.include_router(integrations_router,  prefix="/api/v1", tags=["integrations"])
app.include_router(feedback_router,      prefix="/api/v1", tags=["feedback"])
app.include_router(observe_router,       prefix="/api/v1", tags=["observability"])
app.include_router(costs_router,         prefix="/api/v1", tags=["costs"])
app.include_router(tasks_control_router, prefix="/api/v1", tags=["tasks"])
app.include_router(approval_router,      prefix="/api/v1", tags=["approval"])
app.include_router(whatsapp_router,        prefix="/api/v1", tags=["whatsapp"])
app.include_router(sentry_webhook_router,  prefix="/api/v1", tags=["sentry"])
app.include_router(task_board_router,      prefix="/api/v1", tags=["tasks-board"])
app.include_router(milestones_router,      prefix="/api/v1", tags=["milestones"])
app.include_router(error_api_router,       prefix="/api/v1", tags=["errors"])


@app.get("/", tags=["root"])
async def root():
    return {"status": "Brain is alive", "version": "2.0.0"}
