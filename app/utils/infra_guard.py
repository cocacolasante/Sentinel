"""
InfraGuard — async context manager that snapshots infra state before any write.

Usage:
    async with snapshot_before_write(server, service_name="web") as snap_key:
        # ... execute apt-get / docker compose / certbot
        # If an exception is raised, caller decides whether to rollback
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from app.config import get_settings


@asynccontextmanager
async def snapshot_before_write(server: str, service_name: str | None = None):
    """
    Async context manager: takes an InfraSnapshotSkill snapshot before any
    infra write when sentinel_infra_dry_run is False.

    Yields the snapshot_key (str) or None if dry_run is True.
    The caller is responsible for rollback on exception.
    """
    settings = get_settings()
    snapshot_key: str | None = None

    if not settings.sentinel_infra_dry_run:
        try:
            from app.skills.infra_snapshot_skill import InfraSnapshotSkill
            snapshot_key = await InfraSnapshotSkill().snapshot(server, service_name)
        except Exception:
            pass  # snapshot failure must not block the write

    try:
        yield snapshot_key
    except Exception:
        raise  # caller decides whether to rollback
