"""
PatchAuditSkill — audit and apply OS security patches with CVE scoring.

Trigger intent: patch_audit

Uses apt on Ubuntu. CVE data from ubuntu.com/security API.
Approval gate: posts structured Slack report with Redis pending_approval when
auto_apply=False.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.skills.base import BaseSkill, SkillResult, ApprovalCategory

logger = logging.getLogger(__name__)

_SEVERITY_SCORES = {"critical": 10, "high": 7, "medium": 4, "low": 1, "none": 0}


@dataclass
class AuditedPackage:
    name: str
    version: str
    cve_ids: list[str] = field(default_factory=list)
    score: float = 0.0
    severity: str = "none"


@dataclass
class PatchAuditReport:
    server: str
    total_upgradable: int
    packages: list[AuditedPackage] = field(default_factory=list)
    auto_applied: list[str] = field(default_factory=list)
    requires_reboot: bool = False
    dry_run: bool = True

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "total_upgradable": self.total_upgradable,
            "packages": [
                {"name": p.name, "version": p.version,
                 "cve_ids": p.cve_ids, "score": p.score, "severity": p.severity}
                for p in self.packages
            ],
            "auto_applied": self.auto_applied,
            "requires_reboot": self.requires_reboot,
            "dry_run": self.dry_run,
        }


class PatchAuditSkill(BaseSkill):
    name = "patch_audit"
    description = "Audit and apply OS security patches with CVE scoring"
    trigger_intents = ["patch_audit"]
    approval_category = ApprovalCategory.BREAKING

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        from app.config import get_settings
        settings = get_settings()

        server = params.get("server", "localhost")
        auto_apply = params.get("auto_apply", False)
        dry_run = params.get("dry_run", settings.sentinel_infra_dry_run)
        severity_threshold = params.get("severity_threshold", "medium")

        report = await self._audit(
            server=server,
            auto_apply=auto_apply,
            dry_run=dry_run,
            severity_threshold=severity_threshold,
        )
        return SkillResult(context_data=json.dumps(report.to_dict()))

    async def _audit(
        self,
        server: str = "localhost",
        auto_apply: bool = False,
        dry_run: bool = True,
        severity_threshold: str = "medium",
    ) -> PatchAuditReport:
        from app.config import get_settings
        from app.skills.server_shell_skill import ServerShellSkill
        from app.observability.prometheus_metrics import PATCHES_APPLIED_TOTAL

        settings = get_settings()
        shell = ServerShellSkill()

        # Update apt cache
        await shell.execute({"command": "apt-get update -qq"}, original_message="")

        # Get upgradable packages
        r = await shell.execute(
            {"command": "apt list --upgradable 2>/dev/null | grep -v 'Listing...'"},
            original_message="",
        )
        lines = [l.strip() for l in r.context_data.splitlines() if "/" in l]
        packages_raw = []
        for line in lines:
            parts = line.split("/")
            name = parts[0].strip()
            rest = parts[1] if len(parts) > 1 else ""
            version = rest.split()[0] if rest.split() else "unknown"
            packages_raw.append((name, version))

        # Get OS release for CVE lookups
        r_os = await shell.execute({"command": "cat /etc/os-release"}, original_message="")
        ubuntu_version = "focal"
        for line in r_os.context_data.splitlines():
            if line.startswith("VERSION_CODENAME="):
                ubuntu_version = line.split("=", 1)[1].strip().strip('"')

        # CVE lookups (max 5 concurrent)
        sem = asyncio.Semaphore(5)

        async def _lookup_cves(name: str, version: str) -> AuditedPackage:
            async with sem:
                pkg = AuditedPackage(name=name, version=version)
                try:
                    url = f"{settings.ubuntu_cve_api_base}/cves.json?package={name}&release={ubuntu_version}&limit=5"
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        cves = data.get("cves", data) if isinstance(data, dict) else data
                        if isinstance(cves, list):
                            for cve in cves[:5]:
                                cve_id = cve.get("id", "")
                                sev = (cve.get("cvss", {}).get("severity", "none") or "none").lower()
                                score = _SEVERITY_SCORES.get(sev, 0)
                                if score > pkg.score:
                                    pkg.score = score
                                    pkg.severity = sev
                                if cve_id:
                                    pkg.cve_ids.append(cve_id)
                except Exception:
                    pass
                return pkg

        audited = await asyncio.gather(*[_lookup_cves(n, v) for n, v in packages_raw])
        audited = list(audited)
        audited.sort(key=lambda p: p.score, reverse=True)

        report = PatchAuditReport(
            server=server,
            total_upgradable=len(packages_raw),
            packages=audited,
            dry_run=dry_run,
        )

        if not auto_apply:
            return report

        # Filter by severity threshold
        min_score = _SEVERITY_SCORES.get(severity_threshold.lower(), 4)
        to_apply = [p for p in audited if p.score >= min_score]
        to_apply = to_apply[:settings.sentinel_max_patches_per_run]

        if not to_apply or dry_run:
            return report

        # Apply patches
        from app.utils.infra_guard import snapshot_before_write
        pkg_names = " ".join(p.name for p in to_apply)

        async with snapshot_before_write(server, "packages"):
            r_install = await shell.execute(
                {"command": f"DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade {pkg_names}"},
                original_message="",
            )
            if not r_install.is_error:
                report.auto_applied = [p.name for p in to_apply]
                for p in to_apply:
                    sev = p.severity or "none"
                    try:
                        PATCHES_APPLIED_TOTAL.labels(server=server, severity=sev).inc()
                    except Exception:
                        pass

        # Check reboot required
        r_reboot = await shell.execute(
            {"command": "test -f /var/run/reboot-required && echo REBOOT_REQUIRED || echo OK"},
            original_message="",
        )
        if "REBOOT_REQUIRED" in r_reboot.context_data:
            report.requires_reboot = True
            from app.integrations.slack_notifier import post_alert_sync
            try:
                post_alert_sync(
                    f"⚠️ *Reboot required* on `{server}` after applying patches: `{', '.join(report.auto_applied)}`\n"
                    "Auto-reboot is disabled — please reboot manually.",
                    "sentinel-alerts",
                )
            except Exception:
                pass

        # Write audit entry
        try:
            from app.db.postgres import execute
            await execute(
                "INSERT INTO sentinel_audit (action, target, outcome, detail) VALUES ('patch_applied', $1, 'applied', $2::jsonb)",
                server, json.dumps({"packages": report.auto_applied, "requires_reboot": report.requires_reboot}),
            )
        except Exception:
            pass

        return report
