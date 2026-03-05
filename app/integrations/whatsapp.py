"""
WhatsApp Integration via Twilio

Send and track WhatsApp messages using the Twilio API.
Incoming messages arrive via webhook at POST /api/v1/whatsapp/incoming.

Required .env vars:
  TWILIO_ACCOUNT_SID  — your Twilio Account SID
  TWILIO_AUTH_TOKEN   — your Twilio Auth Token
  TWILIO_WHATSAPP_FROM — your Twilio WhatsApp sender, e.g. whatsapp:+14155238886

Docs: https://www.twilio.com/docs/whatsapp/api
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class WhatsAppClient:
    def __init__(self) -> None:
        self._client = None

    def is_configured(self) -> bool:
        if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_whatsapp_from):
            return False
        try:
            import twilio  # noqa: F401 — check lib is installed

            return True
        except ImportError:
            return False

    def _build_client(self):
        if self._client is None:
            try:
                from twilio.rest import Client
            except ImportError:
                raise RuntimeError("twilio library not installed. Run: pip install twilio")
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        return self._client

    # ── Sync internals ────────────────────────────────────────────────────────

    def _send_sync(self, to: str, body: str) -> dict:
        """Send a WhatsApp message. `to` should be E.164 or whatsapp:+1..."""
        client = self._build_client()
        to_addr = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        msg = client.messages.create(
            from_=settings.twilio_whatsapp_from,
            to=to_addr,
            body=body,
        )
        return {
            "sid": msg.sid,
            "status": msg.status,
            "to": msg.to,
            "body": msg.body,
        }

    def _list_messages_sync(self, to: str | None = None, limit: int = 20) -> list[dict]:
        """List recent WhatsApp messages (sent or received)."""
        client = self._build_client()
        kwargs: dict = {"limit": limit}
        if to:
            to_addr = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
            kwargs["to"] = to_addr
        messages = client.messages.list(**kwargs)
        return [
            {
                "sid": m.sid,
                "from": m.from_,
                "to": m.to,
                "body": m.body,
                "status": m.status,
                "direction": m.direction,
                "date_sent": m.date_sent.isoformat() if m.date_sent else None,
            }
            for m in messages
            if "whatsapp" in (m.from_ or "").lower() or "whatsapp" in (m.to or "").lower()
        ]

    def _get_message_sync(self, sid: str) -> dict:
        client = self._build_client()
        m = client.messages(sid).fetch()
        return {
            "sid": m.sid,
            "from": m.from_,
            "to": m.to,
            "body": m.body,
            "status": m.status,
            "direction": m.direction,
            "date_sent": m.date_sent.isoformat() if m.date_sent else None,
        }

    # ── Public async API ──────────────────────────────────────────────────────

    async def send(self, to: str, body: str) -> dict:
        return await asyncio.to_thread(self._send_sync, to, body)

    async def list_messages(self, to: str | None = None, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._list_messages_sync, to, limit)

    async def get_message(self, sid: str) -> dict:
        return await asyncio.to_thread(self._get_message_sync, sid)
