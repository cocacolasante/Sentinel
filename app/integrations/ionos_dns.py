"""
IONOS DNS API Integration

Manages DNS zones and records via the IONOS Managed DNS API.

Endpoint: https://dns.de-fra.ionos.com
Auth: Same Bearer token or Basic auth as Cloud API.

Docs: https://dns.de-fra.ionos.com/docs
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_BASE = "https://dns.de-fra.ionos.com"
_TIMEOUT = 30.0


def _auth_headers() -> dict:
    if settings.ionos_token:
        return {"Authorization": f"Bearer {settings.ionos_token}"}
    creds = base64.b64encode(f"{settings.ionos_username}:{settings.ionos_password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


class IONOSDNSClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(settings.ionos_token or (settings.ionos_username and settings.ionos_password))

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=_BASE,
                headers={**_auth_headers(), "Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
        return self._client

    # ── Zones ─────────────────────────────────────────────────────────────────

    async def list_zones(self) -> list[dict]:
        r = await self.client.get("/v1/zones")
        r.raise_for_status()
        return [
            {
                "id": z["id"],
                "name": z["properties"].get("zoneName", ""),
                "enabled": z["properties"].get("enabled", True),
            }
            for z in r.json().get("items", [])
        ]

    async def get_zone(self, zone_id: str) -> dict:
        r = await self.client.get(f"/v1/zones/{zone_id}")
        r.raise_for_status()
        return r.json()

    async def find_zone_by_name(self, zone_name: str) -> dict | None:
        zones = await self.list_zones()
        for z in zones:
            if z["name"].rstrip(".").lower() == zone_name.rstrip(".").lower():
                return z
        return None

    async def create_zone(self, zone_name: str) -> dict:
        body = {"properties": {"zoneName": zone_name, "enabled": True}}
        r = await self.client.post("/v1/zones", json=body)
        r.raise_for_status()
        return r.json()

    # ── Records ───────────────────────────────────────────────────────────────

    async def list_records(self, zone_id: str) -> list[dict]:
        r = await self.client.get(f"/v1/zones/{zone_id}/records")
        r.raise_for_status()
        return [
            {
                "id": rec["id"],
                "name": rec["properties"].get("name", ""),
                "type": rec["properties"].get("type", ""),
                "content": rec["properties"].get("content", ""),
                "ttl": rec["properties"].get("ttl", 3600),
                "enabled": rec["properties"].get("enabled", True),
            }
            for rec in r.json().get("items", [])
        ]

    async def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        content: str,
        ttl: int = 3600,
        priority: int | None = None,
    ) -> dict:
        """Create a DNS record. Types: A, AAAA, CNAME, MX, TXT, SRV, NS, CAA, SSHFP."""
        props: dict[str, Any] = {
            "name": name,
            "type": record_type.upper(),
            "content": content,
            "ttl": ttl,
            "enabled": True,
        }
        if priority is not None:
            props["priority"] = priority
        body = {"properties": props}
        r = await self.client.post(f"/v1/zones/{zone_id}/records", json=body)
        r.raise_for_status()
        return r.json()

    async def update_record(self, zone_id: str, record_id: str, fields: dict) -> dict:
        r = await self.client.put(f"/v1/zones/{zone_id}/records/{record_id}", json={"properties": fields})
        r.raise_for_status()
        return r.json()

    async def delete_record(self, zone_id: str, record_id: str) -> dict:
        r = await self.client.delete(f"/v1/zones/{zone_id}/records/{record_id}")
        r.raise_for_status()
        return {"deleted": True, "record_id": record_id}

    # ── Convenience: upsert a record ─────────────────────────────────────────

    async def upsert_record(
        self,
        zone_name: str,
        name: str,
        record_type: str,
        content: str,
        ttl: int = 3600,
    ) -> dict:
        """
        Find or create a zone by name, then create (or update first match) the record.
        Returns the final record dict.
        """
        zone = await self.find_zone_by_name(zone_name)
        if not zone:
            zone = await self.create_zone(zone_name)

        zone_id = zone["id"]
        records = await self.list_records(zone_id)

        # Check for existing record with same name+type
        existing = [
            r for r in records if r["name"].lower() == name.lower() and r["type"].upper() == record_type.upper()
        ]

        if existing:
            return await self.update_record(
                zone_id, existing[0]["id"], {"content": content, "ttl": ttl, "enabled": True}
            )
        return await self.create_record(zone_id, name, record_type, content, ttl)
