"""
PostgreSQL client — thin wrapper around psycopg2.

Provides:
  - get_connection() context manager
  - execute()  — run any SQL, return list[dict]
  - init_schema() — idempotent table creation on startup

Phase 2 note: synchronous psycopg2 is fine for a low-concurrency personal
assistant. Migrate to asyncpg in Phase 5+ if load increases.
"""

import logging
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()


@contextmanager
def get_connection():
    """Yield a psycopg2 connection; auto-commit on success, rollback on error."""
    conn = psycopg2.connect(settings.postgres_dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(sql: str, params=None) -> list[dict]:
    """
    Run a SQL statement and return all rows as list[dict].
    Returns [] for statements that produce no rows (INSERT, UPDATE, DELETE).
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description:
                return [dict(row) for row in cur.fetchall()]
            return []


def execute_one(sql: str, params=None) -> dict | None:
    """Run a SQL statement and return the first row, or None."""
    rows = execute(sql, params)
    return rows[0] if rows else None


def init_schema() -> None:
    """Create all Phase 2 tables if they don't exist (idempotent)."""
    schema_path = Path(__file__).parent / "schema.sql"
    ddl = schema_path.read_text()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        logger.info("Database schema initialised")
    except Exception as exc:
        logger.error("Schema init failed: %s", exc)
        raise


def ping() -> bool:
    """Test the PostgreSQL connection."""
    try:
        execute("SELECT 1")
        return True
    except Exception:
        return False
