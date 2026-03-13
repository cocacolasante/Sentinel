"""
BackupVerifySkill — verify backup recency, size, and optionally restore.

Trigger intent: backup_check

Supports: local (via server_shell ls), s3 (via boto3)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Literal, Optional

from app.skills.base import BaseSkill, SkillResult, ApprovalCategory

logger = logging.getLogger(__name__)

_STALE_HOURS = 25    # > 25h → critical
_WARN_HOURS = 24     # 24–25h → warning
_MIN_SIZE_MB = 1.0   # < 1MB → suspicious


@dataclass
class BackupVerifyResult:
    server: str
    latest_backup_age_hours: float
    latest_backup_size_mb: float
    backup_found: bool
    restore_tested: bool
    restore_succeeded: Optional[bool]
    issues: list[str] = field(default_factory=list)
    status: Literal["healthy", "warning", "critical"] = "healthy"

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "latest_backup_age_hours": self.latest_backup_age_hours,
            "latest_backup_size_mb": self.latest_backup_size_mb,
            "backup_found": self.backup_found,
            "restore_tested": self.restore_tested,
            "restore_succeeded": self.restore_succeeded,
            "issues": self.issues,
            "status": self.status,
        }


class BackupVerifySkill(BaseSkill):
    name = "backup_verify"
    description = "Verify backup recency, size, and optionally test restore"
    trigger_intents = ["backup_check"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        from app.config import get_settings
        settings = get_settings()

        server = params.get("server", "localhost")
        test_restore = params.get("test_restore", False)
        backup_path = params.get("backup_path", "/var/backups/sentinel")

        result = await self._verify(
            server=server,
            backup_path=backup_path,
            test_restore=test_restore,
        )

        # Update Prometheus
        try:
            from app.observability.prometheus_metrics import BACKUP_AGE_HOURS
            BACKUP_AGE_HOURS.labels(server=server).set(result.latest_backup_age_hours)
        except Exception:
            pass

        return SkillResult(context_data=json.dumps(result.to_dict()))

    async def _verify(
        self,
        server: str,
        backup_path: str = "/var/backups/sentinel",
        test_restore: bool = False,
    ) -> BackupVerifyResult:
        from app.config import get_settings
        settings = get_settings()

        issues: list[str] = []
        age_hours: float = 999.0
        size_mb: float = 0.0
        backup_found = False
        restore_tested = False
        restore_succeeded = None

        if settings.backup_storage_type == "s3":
            age_hours, size_mb, backup_found, extra_issues = await self._check_s3(settings)
            issues.extend(extra_issues)
        else:
            age_hours, size_mb, backup_found, extra_issues = await self._check_local(backup_path)
            issues.extend(extra_issues)

        if not backup_found:
            issues.append("No backup files found")
        elif age_hours > _STALE_HOURS:
            issues.append(f"Last backup is {age_hours:.1f}h old (> {_STALE_HOURS}h threshold)")
        elif age_hours > _WARN_HOURS:
            issues.append(f"Last backup is {age_hours:.1f}h old (approaching {_STALE_HOURS}h threshold)")

        if backup_found and size_mb < _MIN_SIZE_MB:
            issues.append(f"Backup size {size_mb:.2f}MB is suspiciously small (< {_MIN_SIZE_MB}MB)")

        if test_restore and backup_found:
            restore_tested = True
            restore_succeeded = await self._test_restore(backup_path, settings)
            if not restore_succeeded:
                issues.append("Restore test FAILED")

        if issues:
            status: Literal["healthy", "warning", "critical"] = (
                "critical" if (not backup_found or age_hours > _STALE_HOURS or restore_succeeded is False)
                else "warning"
            )
        else:
            status = "healthy"

        # Write audit
        try:
            from app.db.postgres import execute
            result_dict = {
                "server": server, "age_hours": age_hours, "size_mb": size_mb,
                "backup_found": backup_found, "restore_succeeded": restore_succeeded,
                "issues": issues, "status": status,
            }
            await execute(
                "INSERT INTO sentinel_audit (action, target, outcome, detail) VALUES ('backup_verified', $1, $2, $3::jsonb)",
                server, status, json.dumps(result_dict),
            )
        except Exception:
            pass

        # Alert if not healthy
        if status in ("warning", "critical"):
            try:
                from app.integrations.slack_notifier import post_alert_sync
                emoji = "⚠️" if status == "warning" else "🚨"
                msg = f"{emoji} *Backup {status.upper()}* — server=`{server}`\n"
                for issue in issues:
                    msg += f"  • {issue}\n"
                post_alert_sync(msg, "sentinel-alerts")
            except Exception:
                pass

        return BackupVerifyResult(
            server=server,
            latest_backup_age_hours=age_hours,
            latest_backup_size_mb=size_mb,
            backup_found=backup_found,
            restore_tested=restore_tested,
            restore_succeeded=restore_succeeded,
            issues=issues,
            status=status,
        )

    async def _check_local(self, backup_path: str) -> tuple[float, float, bool, list[str]]:
        """Check local backup directory via server_shell."""
        from app.skills.server_shell_skill import ServerShellSkill
        import re

        shell = ServerShellSkill()
        issues: list[str] = []

        r = await shell.execute(
            {"command": f"ls -lh {backup_path}/*.dump 2>/dev/null | tail -5 || ls -lh {backup_path}/ 2>/dev/null | tail -5"},
            original_message="",
        )

        if not r.context_data.strip() or "No such file" in r.context_data:
            return 999.0, 0.0, False, ["Backup path not found or empty"]

        # Parse most recent file
        lines = [l for l in r.context_data.splitlines() if l.strip()]
        if not lines:
            return 999.0, 0.0, False, ["No backup files listed"]

        # Try to get age via stat
        last_line = lines[-1]
        size_match = re.search(r'(\d+\.?\d*[KMG]?)\s+\w{3}\s+\d+\s+[\d:]+', last_line)
        size_mb = 0.0
        if size_match:
            raw = size_match.group(1)
            try:
                if raw.endswith("G"):
                    size_mb = float(raw[:-1]) * 1024
                elif raw.endswith("M"):
                    size_mb = float(raw[:-1])
                elif raw.endswith("K"):
                    size_mb = float(raw[:-1]) / 1024
                else:
                    size_mb = float(raw) / (1024 * 1024)
            except ValueError:
                pass

        # Get modification time
        r2 = await shell.execute(
            {"command": f"find {backup_path} -name '*.dump' -printf '%T+ %f\n' 2>/dev/null | sort -r | head -1"},
            original_message="",
        )
        age_hours = 999.0
        if r2.context_data.strip():
            from datetime import datetime, timezone
            try:
                ts_str = r2.context_data.strip().split()[0]
                ts = datetime.strptime(ts_str[:19], "%Y-%m-%d+%H:%M:%S").replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                age_hours = (now - ts).total_seconds() / 3600
            except Exception:
                pass

        return age_hours, size_mb, True, issues

    async def _check_s3(self, settings) -> tuple[float, float, bool, list[str]]:
        """Check S3 backup bucket."""
        issues: list[str] = []
        try:
            import boto3
            from datetime import datetime, timezone

            s3 = boto3.client(
                "s3",
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
            )
            resp = s3.list_objects_v2(
                Bucket=settings.backup_bucket_name,
                Prefix=settings.backup_bucket_prefix,
            )
            objects = resp.get("Contents", [])
            if not objects:
                return 999.0, 0.0, False, ["No objects in S3 backup bucket"]

            objects.sort(key=lambda o: o["LastModified"], reverse=True)
            latest = objects[0]
            now = datetime.now(timezone.utc)
            age_hours = (now - latest["LastModified"]).total_seconds() / 3600
            size_mb = latest["Size"] / (1024 * 1024)
            return age_hours, size_mb, True, issues
        except ImportError:
            return 999.0, 0.0, False, ["boto3 not installed — set backup_storage_type=local or install boto3"]
        except Exception as e:
            return 999.0, 0.0, False, [f"S3 check failed: {e}"]

    async def _test_restore(self, backup_path: str, settings) -> bool:
        """Spin up a temporary Postgres container and attempt a restore."""
        from app.skills.server_shell_skill import ServerShellSkill
        import asyncio
        shell = ServerShellSkill()
        container_name = "sentinel-restore-test"

        try:
            # Find latest backup file
            r = await shell.execute(
                {"command": f"find {backup_path} -name '*.dump' | sort -r | head -1"},
                original_message="",
            )
            backup_file = r.context_data.strip()
            if not backup_file:
                return False

            # Start temp container
            await shell.execute(
                {"command": f"docker run -d --name {container_name} -e POSTGRES_PASSWORD=test postgres:15-alpine"},
                original_message="",
            )
            # Wait for it to start
            await asyncio.sleep(5)

            # Attempt restore
            r_restore = await shell.execute(
                {"command": f"docker exec {container_name} pg_restore -U postgres -d postgres {backup_file} 2>&1 | tail -5"},
                original_message="",
            )
            success = not r_restore.is_error
            return success
        except Exception as e:
            logger.error("Restore test failed: %s", e)
            return False
        finally:
            try:
                await shell.execute(
                    {"command": f"docker rm -f {container_name}"},
                    original_message="",
                )
            except Exception:
                pass
