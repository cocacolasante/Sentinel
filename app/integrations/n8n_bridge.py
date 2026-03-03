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

    # ── Workflow Management (n8n REST API) ────────────────────────────────────

    def _api_client(self) -> "httpx.AsyncClient":
        """Separate client for n8n REST API (different base URL + API key auth)."""
        import httpx as _httpx
        base = f"{settings.n8n_webhook_url}"
        headers: dict = {"Content-Type": "application/json"}
        if settings.n8n_api_key:
            headers["X-N8N-API-KEY"] = settings.n8n_api_key
        return _httpx.AsyncClient(base_url=base, headers=headers, timeout=30.0,
                                  auth=(settings.n8n_user, settings.n8n_password) if not settings.n8n_api_key else None)

    async def list_workflows(self) -> list[dict]:
        """List all workflows via the n8n REST API."""
        async with self._api_client() as c:
            r = await c.get("/api/v1/workflows")
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            return [
                {
                    "id":     w.get("id"),
                    "name":   w.get("name"),
                    "active": w.get("active", False),
                    "nodes":  len(w.get("nodes", [])),
                }
                for w in items
            ]

    async def get_workflow(self, workflow_id: str) -> dict:
        async with self._api_client() as c:
            r = await c.get(f"/api/v1/workflows/{workflow_id}")
            r.raise_for_status()
            return r.json()

    async def create_workflow(self, name: str, nodes: list[dict], connections: dict | None = None) -> dict:
        """Create a new n8n workflow via the REST API."""
        body = {
            "name":        name,
            "nodes":       nodes,
            "connections": connections or {},
            "settings":    {"executionOrder": "v1"},
            "staticData":  None,
        }
        async with self._api_client() as c:
            r = await c.post("/api/v1/workflows", json=body)
            r.raise_for_status()
            data = r.json()
            return {"id": data.get("id"), "name": data.get("name"), "active": data.get("active")}

    async def activate_workflow(self, workflow_id: str) -> dict:
        async with self._api_client() as c:
            r = await c.patch(f"/api/v1/workflows/{workflow_id}/activate")
            r.raise_for_status()
            return {"id": workflow_id, "active": True}

    async def deactivate_workflow(self, workflow_id: str) -> dict:
        async with self._api_client() as c:
            r = await c.patch(f"/api/v1/workflows/{workflow_id}/deactivate")
            r.raise_for_status()
            return {"id": workflow_id, "active": False}

    async def delete_workflow(self, workflow_id: str) -> dict:
        async with self._api_client() as c:
            r = await c.delete(f"/api/v1/workflows/{workflow_id}")
            r.raise_for_status()
            return {"deleted": True, "id": workflow_id}

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
