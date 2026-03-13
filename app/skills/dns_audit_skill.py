"""
DNSAuditSkill — SPF/DKIM/DMARC/MX validation for all monitored domains.

Trigger intent: dns_audit

Requires: dnspython>=2.6
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from app.skills.base import BaseSkill, SkillResult, ApprovalCategory

logger = logging.getLogger(__name__)

_DKIM_SELECTORS = ["google", "default", "mail", "k1", "selector1", "selector2"]


@dataclass
class RecordResult:
    status: Literal["pass", "warn", "fail"]
    value: str
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "value": self.value, "issues": self.issues}


@dataclass
class DNSAuditReport:
    domain: str
    spf: RecordResult
    dmarc: RecordResult
    dkim: RecordResult
    mx: RecordResult
    overall: Literal["pass", "warn", "fail"]
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "spf": self.spf.to_dict(),
            "dmarc": self.dmarc.to_dict(),
            "dkim": self.dkim.to_dict(),
            "mx": self.mx.to_dict(),
            "overall": self.overall,
            "checked_at": self.checked_at.isoformat(),
        }


def _compute_overall(results: list[RecordResult]) -> Literal["pass", "warn", "fail"]:
    statuses = [r.status for r in results]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


class DNSAuditSkill(BaseSkill):
    name = "dns_audit"
    description = "Audit SPF/DKIM/DMARC/MX DNS records for all monitored domains"
    trigger_intents = ["dns_audit"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        action = params.get("action", "audit_all")
        domain = params.get("domain", "")

        if action == "audit" and domain:
            report = await self.audit(domain)
            return SkillResult(context_data=json.dumps(report.to_dict()))
        else:
            reports = await self.audit_all()
            return SkillResult(
                context_data=json.dumps({"reports": [r.to_dict() for r in reports]})
            )

    async def audit(self, domain: str) -> DNSAuditReport:
        """Run full DNS audit for a single domain."""
        try:
            import dns.resolver
            import dns.exception
        except ImportError:
            spf = RecordResult("fail", "", ["dnspython not installed"])
            dmarc = RecordResult("fail", "", ["dnspython not installed"])
            dkim = RecordResult("fail", "", ["dnspython not installed"])
            mx_r = RecordResult("fail", "", ["dnspython not installed"])
            return DNSAuditReport(domain=domain, spf=spf, dmarc=dmarc, dkim=dkim, mx=mx_r, overall="fail")

        loop = asyncio.get_event_loop()

        def _resolve(qname: str, rdtype: str) -> list[str]:
            try:
                answers = dns.resolver.resolve(qname, rdtype, lifetime=10)
                return [rdata.to_text() for rdata in answers]
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.Timeout):
                return []
            except Exception:
                return []

        # SPF
        spf_records = await loop.run_in_executor(None, lambda: _resolve(domain, "TXT"))
        spf_vals = [r for r in spf_records if "v=spf1" in r]
        if not spf_vals:
            spf = RecordResult("fail", "", ["No SPF record found"])
        elif len(spf_vals) > 1:
            spf = RecordResult("warn", spf_vals[0], ["Multiple SPF records found (RFC violation)"])
        else:
            val = spf_vals[0]
            issues = []
            if "+all" in val:
                issues.append("SPF uses +all (too permissive)")
            spf = RecordResult("warn" if issues else "pass", val, issues)

        # DMARC
        dmarc_records = await loop.run_in_executor(None, lambda: _resolve(f"_dmarc.{domain}", "TXT"))
        dmarc_vals = [r for r in dmarc_records if "v=DMARC1" in r]
        if not dmarc_vals:
            dmarc = RecordResult("fail", "", ["No DMARC record found"])
        else:
            val = dmarc_vals[0]
            issues = []
            if "p=none" in val:
                issues.append("DMARC policy is p=none (monitoring only, not enforced)")
            dmarc = RecordResult("warn" if issues else "pass", val, issues)

        # DKIM — try multiple selectors
        dkim_found = None
        for selector in _DKIM_SELECTORS:
            vals = await loop.run_in_executor(
                None, lambda s=selector: _resolve(f"{s}._domainkey.{domain}", "TXT")
            )
            if vals:
                dkim_found = (selector, vals[0])
                break

        if not dkim_found:
            dkim = RecordResult("warn", "", [f"No DKIM record found (tried: {', '.join(_DKIM_SELECTORS)})"])
        else:
            selector, val = dkim_found
            dkim = RecordResult("pass", f"selector={selector} {val[:100]}", [])

        # MX
        mx_records = await loop.run_in_executor(None, lambda: _resolve(domain, "MX"))
        if not mx_records:
            mx_r = RecordResult("fail", "", ["No MX records found"])
        else:
            mx_r = RecordResult("pass", "; ".join(mx_records[:3]), [])

        overall = _compute_overall([spf, dmarc, dkim, mx_r])
        report = DNSAuditReport(domain=domain, spf=spf, dmarc=dmarc, dkim=dkim, mx=mx_r, overall=overall)

        # Persist to DB
        try:
            from app.db.postgres import execute
            await execute(
                "INSERT INTO dns_audit_results (domain, report) VALUES ($1, $2::jsonb)",
                domain, json.dumps(report.to_dict()),
            )
        except Exception as e:
            logger.warning("Failed to save DNS audit for %s: %s", domain, e)

        # Update Prometheus metrics
        try:
            from app.observability.prometheus_metrics import DNS_AUDIT_STATUS
            score_map = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
            for record_type, result in [("spf", spf), ("dmarc", dmarc), ("dkim", dkim), ("mx", mx_r)]:
                DNS_AUDIT_STATUS.labels(domain=domain, record_type=record_type).set(score_map[result.status])
        except Exception:
            pass

        # Alert on failure
        if overall == "fail":
            try:
                from app.integrations.slack_notifier import post_alert_sync
                issues_str = ""
                for rtype, res in [("SPF", spf), ("DMARC", dmarc), ("DKIM", dkim), ("MX", mx_r)]:
                    if res.status == "fail":
                        issues_str += f"  • {rtype}: {', '.join(res.issues)}\n"
                post_alert_sync(
                    f"🚨 *DNS audit FAIL* — `{domain}`\n{issues_str}",
                    "sentinel-alerts",
                )
            except Exception:
                pass

        return report

    async def audit_all(self) -> list[DNSAuditReport]:
        """Audit all domains with dns_monitoring=TRUE (max 10 concurrent)."""
        try:
            from app.db.postgres import execute
            rows = await execute(
                "SELECT domain FROM monitored_domains WHERE dns_monitoring = TRUE ORDER BY domain"
            )
            domains = [r["domain"] for r in (rows or [])]
        except Exception:
            domains = []

        if not domains:
            return []

        sem = asyncio.Semaphore(10)

        async def _audit_one(d: str) -> DNSAuditReport:
            async with sem:
                return await self.audit(d)

        results = await asyncio.gather(*[_audit_one(d) for d in domains], return_exceptions=True)
        return [r for r in results if isinstance(r, DNSAuditReport)]
