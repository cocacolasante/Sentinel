"""
Home Assistant Integration

Uses the HA REST API with a Long-Lived Access Token.

Operations:
  get_all_states()                          — all entity states
  get_entity(entity_id)                     — single entity state + attributes
  call_service(domain, service, data)       — control a device
  list_entities(domain)                     — filter entities by domain
  get_history(entity_id, hours)             — state history

Common domains: light, switch, climate, media_player, lock, cover,
                input_boolean, script, automation, sensor, binary_sensor

Common services:
  light.turn_on / light.turn_off / light.toggle
  switch.turn_on / switch.turn_off
  climate.set_temperature / climate.set_hvac_mode
  lock.lock / lock.unlock
  media_player.play_media / media_player.media_pause
  script.turn_on
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_TIMEOUT = 10.0


class HomeAssistantClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(settings.home_assistant_url and settings.home_assistant_token)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.home_assistant_token}",
            "Content-Type": "application/json",
        }

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{settings.home_assistant_url}/api",
                headers=self._headers(),
                timeout=_TIMEOUT,
                verify=settings.home_assistant_verify_ssl,
            )
        return self._client

    async def _get(self, path: str) -> Any:
        r = await self.client.get(path)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, data: dict | None = None) -> Any:
        r = await self.client.post(path, json=data or {})
        r.raise_for_status()
        return r.json()

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_all_states(self) -> list[dict]:
        """Return all entity states (trimmed for LLM context)."""
        data = await self._get("/states")
        return [
            {
                "entity_id": s.get("entity_id"),
                "state": s.get("state"),
                "name": s.get("attributes", {}).get("friendly_name", s.get("entity_id")),
                "last_changed": s.get("last_changed"),
            }
            for s in data
        ]

    async def get_entity(self, entity_id: str) -> dict:
        """Return the full state of a single entity."""
        data = await self._get(f"/states/{entity_id}")
        return {
            "entity_id": data.get("entity_id"),
            "state": data.get("state"),
            "attributes": data.get("attributes", {}),
            "last_changed": data.get("last_changed"),
        }

    async def call_service(self, domain: str, service: str, data: dict) -> dict:
        """
        Call a HA service to control a device.

        Example:
          await ha.call_service("light", "turn_on", {"entity_id": "light.living_room", "brightness": 200})
        """
        result = await self._post(f"/services/{domain}/{service}", data)
        # HA returns the list of affected states
        affected = [s.get("entity_id") for s in result] if isinstance(result, list) else []
        return {
            "success": True,
            "domain": domain,
            "service": service,
            "affected": affected,
        }

    async def list_entities(self, domain: str | None = None) -> list[dict]:
        """List entities, optionally filtered by domain (e.g. 'light', 'switch')."""
        all_states = await self.get_all_states()
        if domain:
            return [s for s in all_states if s["entity_id"].startswith(f"{domain}.")]
        return all_states

    async def get_history(self, entity_id: str, hours: int = 24) -> list[dict]:
        """Return state history for an entity over the last N hours."""
        start = (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).isoformat()
        data = await self._get(f"/history/period/{start}?filter_entity_id={entity_id}")
        if not data or not data[0]:
            return []
        return [
            {
                "state": h.get("state"),
                "changed_at": h.get("last_changed"),
            }
            for h in data[0]
        ]

    async def health(self) -> bool:
        """Check HA connectivity."""
        try:
            r = await self.client.get("/")
            return r.status_code == 200
        except Exception:
            return False
