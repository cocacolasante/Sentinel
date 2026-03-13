"""
CertMonitorSkill — TLS certificate expiry checks and certbot renewal.

Trigger intent: cert_check

Uses Python stdlib ssl (no new dependencies) for TLS checks.
Alert deduplication via Redis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
from datetime import datetime, timezone
from typing import Literal

from app.skills.base import BaseSkill, SkillResult, ApprovalCategory

logger = logging.getLogger(__name__)

_WARN_TTL = 7 * 86400    # 7 days — deduplicate warning alerts
_CRIT_TTL = 86400        # 24 hours — re-post critical daily


class CertMonitorSkill(BaseSkill):
    name = "cert_monitor"
    description = "Check SSL/TLS certificate expiry and trigger certbot renewal"
    trigger_intents = ["cert_check"]
    approval_category = ApprovalCategory.CRITICAL

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        action = params.get("action", "check_all")
        domain = params.get("domain", "")

        if action == "check" and domain:
            result = await self.check(domain)
            return SkillResult(context_data=json.dumps(result))
        elif action == "renew" and domain:
            server = params.get("server", "localhost")
            result = await self.renew(domain, server)
            return SkillResult(context_data=json.dumps(result))
        else:
            results = await self.check_all()
            return SkillResult(context_data=json.dumps({"results": results}))

    async def check(self, domain: str) -> dict:
        """
        Check TLS cert expiry for a domain on port 443.
        Updates monitored_domains table.
        Returns dict with status, days_remaining, expiry_date.
        """
        from app.config import get_settings
        from app.db.postgres import execute
        from app.observability.prometheus_metrics import CERT_DAYS_REMAINING

        settings = get_settings()
        result = {
            "domain": domain,
            "status": "unknown",
            "days_remaining": None,
            "expiry_date": None,
            "error": None,
        }

        try:
            context = ssl.create_default_context()
            loop = asyncio.get_event_loop()

            def _get_cert():
                with socket.create_connection((domain, 443), timeout=10) as sock:
                    with context.wrap_socket(sock, server_hostname=domain) as ssock:
                        cert = ssock.getpeercert()
                        return cert

            cert = await loop.run_in_executor(None, _get_cert)
            not_after_str = cert["notAfter"]
            expiry = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z")
            expiry = expiry.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            days_remaining = (expiry - now).days

            if days_remaining <= settings.sentinel_cert_critical_days:
                status = "critical"
            elif days_remaining <= settings.sentinel_cert_warning_days:
                status = "warning"
            else:
                status = "healthy"

            result.update({
                "days_remaining": days_remaining,
                "expiry_date": expiry.isoformat(),
                "status": status,
            })

            # Update metrics
            try:
                CERT_DAYS_REMAINING.labels(domain=domain).set(days_remaining)
            except Exception:
                pass

            # Update DB
            try:
                await execute(
                    """
                    INSERT INTO monitored_domains (domain, last_checked, expiry_date, days_remaining, status)
                    VALUES ($1, NOW(), $2, $3, $4)
                    ON CONFLICT (domain) DO UPDATE SET
                        last_checked   = NOW(),
                        expiry_date    = EXCLUDED.expiry_date,
                        days_remaining = EXCLUDED.days_remaining,
                        status         = EXCLUDED.status
                    """,
                    domain, expiry, days_remaining, status,
                )
            except Exception as e:
                logger.warning("Failed to update monitored_domains for %s: %s", domain, e)

            # Alert with deduplication
            await self._maybe_alert(domain, status, days_remaining)

            # Auto-renew if critical and auto_renew is set
            try:
                rows = await execute(
                    "SELECT auto_renew, server_hostname FROM monitored_domains WHERE domain = $1",
                    domain,
                )
                if rows and rows[0]["auto_renew"] and status == "critical":
                    server = rows[0].get("server_hostname") or "localhost"
                    await self.renew(domain, server)
            except Exception:
                pass

        except Exception as e:
            result["error"] = str(e)
            result["status"] = "error"
            logger.error("Cert check failed for %s: %s", domain, e)

        return result

    async def _maybe_alert(self, domain: str, status: str, days_remaining: int) -> None:
        from app.db.redis import get_redis
        from app.integrations.slack_notifier import post_alert_sync

        if status == "healthy":
            return

        redis = await get_redis()
        alert_key = f"sentinel:cert_alert:{domain}:{status}"

        ttl = _WARN_TTL if status == "warning" else _CRIT_TTL

        already_alerted = await redis.exists(alert_key)
        if already_alerted:
            return

        emoji = "⚠️" if status == "warning" else "🚨"
        msg = f"{emoji} *SSL cert {status.upper()}* — `{domain}` expires in {days_remaining} days"
        try:
            post_alert_sync(msg, "sentinel-alerts")
        except Exception:
            pass

        await redis.setex(alert_key, ttl, "1")

    async def check_all(self) -> list[dict]:
        """Check all domains with auto_renew=TRUE (max 5 concurrent)."""
        from app.db.postgres import execute

        try:
            rows = await execute(
                "SELECT domain FROM monitored_domains WHERE auto_renew = TRUE ORDER BY domain"
            )
            domains = [r["domain"] for r in (rows or [])]
        except Exception:
            domains = []

        if not domains:
            return []

        # Max 5 concurrent
        sem = asyncio.Semaphore(5)

        async def _check_one(d: str) -> dict:
            async with sem:
                return await self.check(d)

        results = await asyncio.gather(*[_check_one(d) for d in domains], return_exceptions=True)
        return [r if isinstance(r, dict) else {"domain": "unknown", "error": str(r)} for r in results]

    async def renew(self, domain: str, server: str = "localhost") -> dict:
        """Run certbot renew for a domain via server_shell."""
        from app.skills.server_shell_skill import ServerShellSkill
        from app.db.postgres import execute
        from app.integrations.slack_notifier import post_alert_sync

        shell = ServerShellSkill()
        cmd = f"certbot renew --cert-name {domain} --non-interactive 2>&1"
        result = await shell.execute({"command": cmd}, original_message="")

        success = not result.is_error and "error" not in result.context_data.lower()
        status = "renewed" if success else "renewal_failed"

        # Write audit
        try:
            await execute(
                "INSERT INTO sentinel_audit (action, target, outcome, detail) VALUES ('cert_renew', $1, $2, $3::jsonb)",
                domain, status, json.dumps({"output": result.context_data[:2000]}),
            )
        except Exception:
            pass

        if not success:
            try:
                from app.db.redis import get_redis
                redis = await get_redis()
                alert_key = f"sentinel:cert_alert:{domain}:renewal_failed"
                await redis.delete(alert_key)  # always re-alert on failure
            except Exception:
                pass
            try:
                post_alert_sync(
                    f"🚨 *Cert renewal FAILED* — `{domain}`\n```{result.context_data[:500]}```",
                    "sentinel-alerts",
                )
            except Exception:
                pass

        return {"domain": domain, "status": status, "output": result.context_data[:2000]}
