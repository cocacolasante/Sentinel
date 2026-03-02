"""
n8n Bridge — the Brain's action executor.

The Brain calls n8n webhooks to execute multi-step workflows:
  - email send
  - calendar create
  - complex multi-service automations
  - anything needing retry logic or visual debugging

n8n does NOT initiate conversations or make decisions.
The Brain decides what to do; n8n does it.

Webhook URL pattern:
  POST {N8N_WEBHOOK_URL}/webhook/{workflow_name}
  Body: {"action": "...", "payload": {...}}
  Auth: HTTP Basic (N8N_USER / N8N_PASSWORD)

Response:
  {"success": true, "result": {...}}  or  {"success": false, "error": "..."}
"""

import logging

import httpx

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

_TIMEOUT = 30.0


class N8nBridge:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(settings.n8n_webhook_url)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                auth=(settings.n8n_user, settings.n8n_password),
                timeout=_TIMEOUT,
            )
        return self._client

    async def trigger(self, workflow: str, payload: dict) -> dict:
        """
        Trigger an n8n webhook workflow by name.

        Args:
            workflow: The webhook path / workflow name (e.g. "send-email")
            payload:  JSON body sent to the workflow

        Returns:
            The JSON response from n8n, or an error dict.
        """
        url = f"{settings.n8n_webhook_url}/webhook/{workflow}"
        try:
            r = await self.client.post(url, json={"action": workflow, "payload": payload})
            r.raise_for_status()
            return r.json() if r.content else {"success": True}
        except httpx.HTTPStatusError as exc:
            logger.error("n8n webhook %s returned %s: %s", workflow, exc.response.status_code, exc.response.text)
            return {"success": False, "error": f"HTTP {exc.response.status_code}: {exc.response.text}"}
        except Exception as exc:
            logger.error("n8n bridge error (%s): %s", workflow, exc)
            return {"success": False, "error": str(exc)}

    async def send_email(self, to: str, subject: str, body: str) -> dict:
        """Convenience wrapper — trigger the 'brain/send-email' n8n workflow."""
        return await self.trigger("brain/send-email", {
            "to":      to,
            "subject": subject,
            "body":    body,
        })

    async def create_calendar_event(self, event: dict) -> dict:
        """Convenience wrapper — trigger the 'brain/calendar-create' n8n workflow."""
        return await self.trigger("brain/calendar-create", event)

    async def run_daily_brief(self) -> dict:
        """Trigger the daily briefing workflow."""
        return await self.trigger("brain/daily-brief", {})

    async def health(self) -> bool:
        """Check if n8n is reachable."""
        try:
            r = await self.client.get(
                f"{settings.n8n_webhook_url}/healthz",
                timeout=5.0,
            )
            return r.status_code < 400
        except Exception:
            return False
