"""
AI Brain — FastAPI entry point.

Startup sequence:
  1. PostgreSQL schema initialised (idempotent)
  2. Slack Socket Mode connection launched as background task
  3. REST endpoints available at /api/v1/

Routes:
  GET  /                              -- health probe
  GET  /api/v1/health                 -- detailed health (Redis + Postgres)
  POST /api/v1/chat                   -- main chat endpoint (intent-routed)
  DELETE /api/v1/chat/{id}            -- clear session history
  GET  /api/v1/integrations/status    -- integration health check
  GET  /api/v1/integrations/...       -- direct integration endpoints
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.router import chat
from app.router.integrations import router as integrations_router
from app.router.feedback     import router as feedback_router
from app.router.slack        import start_socket_mode
from app.config              import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger   = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Brain starting -- environment: %s", settings.environment)

    # Initialise PostgreSQL schema (creates tables if they don't exist)
    try:
        from app.db import postgres
        await asyncio.to_thread(postgres.init_schema)
        logger.info("PostgreSQL ready")
    except Exception as exc:
        logger.error("PostgreSQL init failed -- check POSTGRES_* env vars: %s", exc)

    # Initialise Qdrant collection
    try:
        from app.memory.qdrant_client import QdrantMemory
        qm = QdrantMemory(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            collection=settings.qdrant_collection,
        )
        await qm.init_collection()
        logger.info("Qdrant ready")
    except Exception as exc:
        logger.error("Qdrant init failed (non-fatal): %s", exc)

    # Launch Slack Socket Mode in the background (non-blocking)
    asyncio.create_task(start_socket_mode())

    yield

    logger.info("Brain shutting down")


app = FastAPI(
    title="AI Brain",
    description="Personalized AI Assistant -- CSuite Code",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.include_router(chat.router,         prefix="/api/v1", tags=["chat"])
app.include_router(integrations_router, prefix="/api/v1", tags=["integrations"])
app.include_router(feedback_router,     prefix="/api/v1", tags=["feedback"])


@app.get("/", tags=["root"])
async def root():
    return {"status": "Brain is alive", "version": "2.0.0"}
