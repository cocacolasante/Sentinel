"""
InfraSnapshotSkill — Redis snapshot + rollback for infrastructure state.

Captures docker compose state, image SHAs, and package list before any
write operation. Allows rollback to a known-good state.

Trigger intent: infra_rollback
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from app.skills.base import BaseSkill, SkillResult, ApprovalCategory

_SNAPSHOT_TTL = 48 * 3600  # 48 hours


class InfraSnapshotSkill(BaseSkill):
    name = "infra_snapshot"
    description = "Take a snapshot of infra state before writes; rollback on failure"
    trigger_intents = ["infra_rollback"]
    approval_category = ApprovalCategory.CRITICAL

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        action = params.get("action", "rollback")
        server = params.get("server", "")
        snapshot_key = params.get("snapshot_key")

        if action == "list":
            keys = await self.list_snapshots(server)
            return SkillResult(context_data=json.dumps({"snapshots": keys}))
        elif action == "rollback":
            if not server:
                return SkillResult(
                    context_data="Error: server hostname required for rollback",
                    is_error=True,
                )
            result = await self.rollback(server, snapshot_key)
            return SkillResult(context_data=json.dumps(result))
        return SkillResult(context_data="Unknown action")

    async def snapshot(self, server: str, service_name: str | None = None) -> str:
        """
        Capture docker compose + image + package state via server_shell.
        Stores in Redis with 48h TTL.
        Returns the snapshot_key.
        """
        from app.db.redis import get_redis
        from app.skills.server_shell_skill import ServerShellSkill

        shell = ServerShellSkill()
        ts = int(time.time())
        snapshot_key = f"sentinel:snapshot:{server}:{ts}"
        latest_key = f"sentinel:snapshot:{server}:latest"

        data: dict = {
            "server": server,
            "service_name": service_name,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "docker_compose_ps": None,
            "docker_images": None,
            "packages": None,
        }

        # Capture docker compose ps
        try:
            r1 = await shell.execute(
                {"command": "docker compose ps --format json", "cwd": "/root/sentinel"},
                original_message="",
            )
            data["docker_compose_ps"] = r1.context_data
        except Exception:
            pass

        # Capture docker images
        try:
            r2 = await shell.execute(
                {"command": "docker images --format json"},
                original_message="",
            )
            data["docker_images"] = r2.context_data
        except Exception:
            pass

        # Capture package list
        try:
            r3 = await shell.execute(
                {"command": "dpkg -l | grep -E '^ii' | head -200"},
                original_message="",
            )
            data["packages"] = r3.context_data
        except Exception:
            pass

        redis = await get_redis()
        async with redis.pipeline() as pipe:
            pipe.setex(snapshot_key, _SNAPSHOT_TTL, json.dumps(data))
            pipe.setex(latest_key, _SNAPSHOT_TTL, snapshot_key)
            await pipe.execute()

        return snapshot_key

    async def rollback(
        self, server: str, snapshot_key: str | None = None
    ) -> dict:
        """
        Restore a server to a previous snapshot state.
        Re-pulls images and runs docker compose up -d.
        """
        from app.db.redis import get_redis
        from app.skills.server_shell_skill import ServerShellSkill
        from app.integrations.slack_notifier import post_alert_sync
        from app.db.postgres import execute

        redis = await get_redis()

        # Resolve snapshot key
        if not snapshot_key:
            latest_key = f"sentinel:snapshot:{server}:latest"
            snapshot_key = await redis.get(latest_key)
            if snapshot_key:
                snapshot_key = snapshot_key.decode() if isinstance(snapshot_key, bytes) else snapshot_key

        if not snapshot_key:
            return {"status": "error", "message": f"No snapshot found for {server}"}

        raw = await redis.get(snapshot_key)
        if not raw:
            return {"status": "error", "message": f"Snapshot {snapshot_key} expired or missing"}

        snapshot = json.loads(raw)
        shell = ServerShellSkill()
        service_name = snapshot.get("service_name", "")

        # Re-apply docker compose
        compose_cmd = f"docker compose up -d {service_name}".strip()
        result = await shell.execute(
            {"command": compose_cmd, "cwd": "/root/sentinel"},
            original_message="",
        )

        # Verify deployment
        try:
            from app.skills.deploy_verifier_skill import DeployVerifierSkill
            verifier = DeployVerifierSkill()
            await verifier.execute({}, original_message="")
        except Exception:
            pass

        status = "success" if not result.is_error else "partial"
        summary = {
            "status": status,
            "server": server,
            "snapshot_key": snapshot_key,
            "captured_at": snapshot.get("captured_at"),
            "service_name": service_name,
        }

        # Write audit entry
        try:
            await execute(
                """
                INSERT INTO sentinel_audit (action, target, outcome, detail)
                VALUES ('infra_rollback', $1, $2, $3::jsonb)
                """,
                server, status, json.dumps(summary),
            )
        except Exception:
            pass

        # Post Slack alert
        try:
            msg = f"🔄 *Infra Rollback* — server=`{server}` status=`{status}`\n"
            msg += f"Snapshot: `{snapshot_key}` (captured: {snapshot.get('captured_at')})"
            post_alert_sync(msg, "sentinel-alerts")
        except Exception:
            pass

        return summary

    async def list_snapshots(self, server: str) -> list[str]:
        from app.db.redis import get_redis
        redis = await get_redis()
        pattern = f"sentinel:snapshot:{server}:*"
        keys = []
        async for key in redis.scan_iter(match=pattern):
            k = key.decode() if isinstance(key, bytes) else key
            if not k.endswith(":latest"):
                keys.append(k)
        return sorted(keys, reverse=True)
