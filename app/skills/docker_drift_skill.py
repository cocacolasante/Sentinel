"""
DockerDriftSkill — detects Docker Compose drift and optionally auto-corrects.

Trigger intent: docker_drift

Wraps existing server_shell for all SSH execution.
Never auto-removes rogue containers — always requires human review.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from app.skills.base import BaseSkill, SkillResult, ApprovalCategory

logger = logging.getLogger(__name__)


@dataclass
class DriftIssue:
    type: str          # "missing_service" | "image_drift" | "env_drift" | "rogue_container"
    service: str
    expected: str
    actual: str


@dataclass
class DriftReport:
    server: str
    status: Literal["clean", "drifted"]
    issues: list[DriftIssue] = field(default_factory=list)
    auto_corrected: list[str] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "status": self.status,
            "issues": [
                {"type": i.type, "service": i.service, "expected": i.expected, "actual": i.actual}
                for i in self.issues
            ],
            "auto_corrected": self.auto_corrected,
            "dry_run": self.dry_run,
        }


class DockerDriftSkill(BaseSkill):
    name = "docker_drift"
    description = "Check and optionally fix Docker Compose drift on a server"
    trigger_intents = ["docker_drift"]
    approval_category = ApprovalCategory.CRITICAL

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        from app.config import get_settings
        settings = get_settings()

        server = params.get("server", "localhost")
        auto_correct = params.get("auto_correct", False)
        dry_run = params.get("dry_run", settings.sentinel_infra_dry_run)

        report = await self._check_drift(server, auto_correct=auto_correct, dry_run=dry_run)
        return SkillResult(context_data=json.dumps(report.to_dict()))

    async def _check_drift(
        self,
        server: str,
        auto_correct: bool = False,
        dry_run: bool = True,
    ) -> DriftReport:
        from app.skills.server_shell_skill import ServerShellSkill
        from app.integrations.slack_notifier import post_alert_sync
        from app.observability.prometheus_metrics import DOCKER_DRIFT_ISSUES

        shell = ServerShellSkill()
        issues: list[DriftIssue] = []
        auto_corrected: list[str] = []

        # Get desired state from docker compose config
        desired_services: set[str] = set()
        try:
            r = await shell.execute(
                {"command": "docker compose ps --format '{{.Service}}' 2>/dev/null || docker compose config --services",
                 "cwd": "/root/sentinel"},
                original_message="",
            )
            for line in r.context_data.splitlines():
                svc = line.strip()
                if svc and not svc.startswith("{"):
                    desired_services.add(svc)
        except Exception as e:
            logger.warning("Failed to get docker compose services: %s", e)

        # Get actual running containers
        running_services: dict[str, str] = {}
        try:
            r2 = await shell.execute(
                {"command": "docker ps --format '{{.Names}}\\t{{.Status}}'"},
                original_message="",
            )
            for line in r2.context_data.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    running_services[parts[0]] = parts[1]
        except Exception as e:
            logger.warning("Failed to get running containers: %s", e)

        # Detect missing services
        for svc in desired_services:
            if svc not in running_services:
                issues.append(DriftIssue(
                    type="missing_service",
                    service=svc,
                    expected="running",
                    actual="stopped/missing",
                ))

        # Detect rogue containers (running but not in compose)
        for container, status in running_services.items():
            # Skip system containers
            if any(x in container for x in ["_run_", "restore-test"]):
                continue
            is_compose_managed = any(svc in container for svc in desired_services)
            if not is_compose_managed and desired_services:
                issues.append(DriftIssue(
                    type="rogue_container",
                    service=container,
                    expected="not in compose",
                    actual=status,
                ))

        # Post Slack alert for rogue containers (always, regardless of auto_correct)
        rogue = [i for i in issues if i.type == "rogue_container"]
        if rogue:
            msg = f"⚠️ *Rogue containers detected on `{server}`*:\n"
            for issue in rogue:
                msg += f"  • `{issue.service}` — {issue.actual}\n"
            msg += "Manual review required — auto-removal disabled."
            try:
                post_alert_sync(msg, "sentinel-alerts")
            except Exception:
                pass

        # Auto-correct missing services (never rogue containers)
        if auto_correct and not dry_run:
            missing = [i for i in issues if i.type == "missing_service"]
            if missing:
                from app.utils.infra_guard import snapshot_before_write
                async with snapshot_before_write(server, "docker"):
                    for issue in missing:
                        try:
                            await shell.execute(
                                {"command": f"docker compose up -d {issue.service}",
                                 "cwd": "/root/sentinel"},
                                original_message="",
                            )
                            auto_corrected.append(issue.service)
                        except Exception as e:
                            logger.error("Failed to restart %s: %s", issue.service, e)

        # Update Prometheus metrics
        for issue in issues:
            try:
                DOCKER_DRIFT_ISSUES.labels(server=server, issue_type=issue.type).set(1)
            except Exception:
                pass

        status: Literal["clean", "drifted"] = "drifted" if issues else "clean"
        return DriftReport(
            server=server,
            status=status,
            issues=issues,
            auto_corrected=auto_corrected,
            dry_run=dry_run,
        )
