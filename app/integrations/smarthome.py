"""
SmartHomeClient — thin wrapper over HomeAssistantClient.

Exposes a clean, intent-friendly API used by SmartHomeSkill and any
future agent that needs device control or Alexa TTS announcements.

Methods:
  is_configured()                     → bool
  turn_on(entity_id)                  → dict
  turn_off(entity_id)                 → dict
  get_states(domain=None)             → list[dict]
  call_service(domain, service, data) → dict
  announce(message, target=None)      → dict
  health()                            → bool
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _domain_from(entity_id: str) -> str:
    """Extract HA domain from entity_id, e.g. 'light.living_room' → 'light'."""
    return entity_id.split(".")[0] if "." in entity_id else "homeassistant"


class SmartHomeClient:
    """High-level smart home interface backed by HomeAssistantClient."""

    def is_configured(self) -> bool:
        from app.integrations.home_assistant import HomeAssistantClient
        return HomeAssistantClient().is_configured()

    async def turn_on(self, entity_id: str) -> dict:
        from app.integrations.home_assistant import HomeAssistantClient
        return await HomeAssistantClient().call_service(
            _domain_from(entity_id), "turn_on", {"entity_id": entity_id}
        )

    async def turn_off(self, entity_id: str) -> dict:
        from app.integrations.home_assistant import HomeAssistantClient
        return await HomeAssistantClient().call_service(
            _domain_from(entity_id), "turn_off", {"entity_id": entity_id}
        )

    async def get_states(self, domain: str | None = None) -> list[dict]:
        from app.integrations.home_assistant import HomeAssistantClient
        return await HomeAssistantClient().list_entities(domain)

    async def call_service(self, domain: str, service: str, data: dict) -> dict:
        from app.integrations.home_assistant import HomeAssistantClient
        return await HomeAssistantClient().call_service(domain, service, data)

    async def announce(self, message: str, target: str | None = None) -> dict:
        """
        Send a TTS announcement to Alexa Echo devices via the
        Alexa Media Player HACS integration.

        Returns an error dict if the integration is not installed.
        """
        from app.integrations.home_assistant import HomeAssistantClient
        try:
            return await HomeAssistantClient().call_service(
                "alexa_media",
                "send_announcement",
                {
                    "message": message,
                    "target": target or "all",
                    "data": {"type": "announce"},
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("alexa_media service not found — Alexa Media Player not installed")
                return {
                    "success": False,
                    "error": "alexa_media not installed — see ALEXA_HA_SETUP.md",
                }
            raise

    async def health(self) -> bool:
        from app.integrations.home_assistant import HomeAssistantClient
        return await HomeAssistantClient().health()
