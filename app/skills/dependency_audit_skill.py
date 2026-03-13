"""
DependencyAuditSkill — scan requirements.txt or package.json against the OSV API
for known CVEs and security advisories.

Intent: audit_deps
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import aiohttp

from app.config import get_settings
from app.db import postgres
from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)
settings = get_settings()

_OSV_API = "https://api.osv.dev/v1/query"


def _parse_requirements_txt(content: str) -> list[tuple[str, str]]:
    """Parse requirements.txt into list of (package, version)."""
    packages: list[tuple[str, str]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Match: package==1.2.3 or package>=1.2.3 etc.
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*[=><~!]+\s*([^\s;]+)", line)
        if match:
            packages.append((match.group(1), match.group(2)))
        else:
            # Package without version pin
            name_match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
            if name_match:
                packages.append((name_match.group(1), ""))
    return packages


def _parse_package_json(content: str) -> list[tuple[str, str]]:
    """Parse package.json into list of (package, version)."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    packages: list[tuple[str, str]] = []
    for section in ("dependencies", "devDependencies"):
        for name, ver in data.get(section, {}).items():
            # Strip leading ^, ~, etc.
            clean_ver = re.sub(r"^[^0-9]*", "", ver)
            packages.append((name, clean_ver))
    return packages


async def _query_osv(session, pkg_name: str, version: str, ecosystem: str) -> dict:
    """Query OSV API for a single package. Returns raw OSV response dict."""
    payload: dict = {"package": {"name": pkg_name, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version
    try:
        async with session.post(_OSV_API, json=payload, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as exc:
        logger.debug("OSV query failed for %s: %s", pkg_name, exc)
    return {}


class DependencyAuditSkill(BaseSkill):
    name = "dependency_audit"
    description = (
        "Audit Python (requirements.txt) or Node.js (package.json) dependencies against "
        "the OSV database for known CVEs and security advisories. "
        "Use for: 'audit dependencies', 'check for CVEs', 'scan packages for vulnerabilities'."
    )
    trigger_intents = ["audit_deps"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        repo_path = params.get("repo_path", "/root/sentinel-workspace")
        session_id = params.get("session_id", "")
        root = Path(repo_path)

        # Detect manifest
        packages: list[tuple[str, str]] = []
        ecosystem = "PyPI"
        manifest_file = ""

        req_txt = root / "requirements.txt"
        pkg_json = root / "package.json"

        if req_txt.exists():
            packages = _parse_requirements_txt(req_txt.read_text())
            manifest_file = "requirements.txt"
            ecosystem = "PyPI"
        elif pkg_json.exists():
            packages = _parse_package_json(pkg_json.read_text())
            manifest_file = "package.json"
            ecosystem = "npm"
        else:
            return SkillResult(
                context_data=f"No requirements.txt or package.json found in {repo_path}",
                is_error=True,
            )

        if not packages:
            return SkillResult(context_data=f"No packages found in {manifest_file}")

        cve_findings: list[dict] = []

        async with aiohttp.ClientSession() as session:
            tasks = [_query_osv(session, name, ver, ecosystem) for name, ver in packages[:100]]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for (pkg_name, pkg_ver), osv_resp in zip(packages[:100], results):
            if isinstance(osv_resp, Exception):
                continue
            vulns = osv_resp.get("vulns", [])
            for vuln in vulns:
                cve_id = vuln.get("id", "")
                summary = vuln.get("summary", "")[:200]
                severity = ""
                for db_specific in vuln.get("database_specific", {}).values():
                    if isinstance(db_specific, str):
                        severity = db_specific
                aliases = vuln.get("aliases", [])
                finding = {
                    "package": pkg_name,
                    "version": pkg_ver,
                    "cve_id": cve_id,
                    "summary": summary,
                    "severity": severity,
                    "aliases": aliases[:3],
                }
                cve_findings.append(finding)

                # Write sentinel_audit row per CVE
                try:
                    postgres.execute(
                        """
                        INSERT INTO sentinel_audit (session_id, action, target, outcome, detail)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            "audit_deps",
                            f"{pkg_name}@{pkg_ver}",
                            "cve_found",
                            json.dumps(finding),
                        ),
                    )
                except Exception as exc:
                    logger.debug("sentinel_audit insert failed: %s", exc)

        # Format report
        if not cve_findings:
            report = f"✅ No CVEs found in {manifest_file} ({len(packages)} packages scanned)."
        else:
            lines = [
                f"⚠️ **{len(cve_findings)} CVE(s) found** in {manifest_file} ({len(packages)} packages scanned)\n",
                f"{'Package':<30} {'Version':<12} {'CVE ID':<20} Summary",
                "-" * 90,
            ]
            for f in cve_findings[:30]:
                lines.append(
                    f"{f['package']:<30} {f['version']:<12} {f['cve_id']:<20} {f['summary'][:40]}"
                )
            report = "\n".join(lines)

        return SkillResult(
            context_data=json.dumps({"report": report, "cve_count": len(cve_findings), "findings": cve_findings[:20]})
        )
