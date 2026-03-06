"""
MeshCentral Integration Client

Hybrid model:
  Primary:   WebSocket subscription for real-time agent events
  Secondary: HTTP REST API for device inventory + remote commands
  Tertiary:  Event log stored in Postgres for audit/replay/AI context

Auth: session cookie obtained via POST /login (username + password).
The domain field is required for multi-tenant MeshCentral instances.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_WS_PING_INTERVAL = 30
_WS_PING_TIMEOUT = 10
_MAX_RECONNECT_DELAY = 120


def _settings():
    from app.config import get_settings
    return get_settings()


class MeshCentralClient:
    """
    Async client for MeshCentral management API.

    Provides:
    - Session-based REST API: device inventory, run commands, power actions
    - WebSocket event stream: real-time agent connect/disconnect/alerts
    - Agent install script generation for post-provision automation
    """

    def __init__(self) -> None:
        s = _settings()
        self.url = s.meshcentral_url.rstrip("/")
        self.username = s.meshcentral_user
        self.password = s.meshcentral_password
        self.domain = s.meshcentral_domain or ""
        self._session_cookie: Optional[str] = None

    def is_configured(self) -> bool:
        return bool(self.url and self.username and self.password)

    # ── Authentication ────────────────────────────────────────────────────────

    async def _login(self) -> bool:
        """Authenticate with MeshCentral and cache the session cookie."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as client:
                resp = await client.post(
                    f"{self.url}/login",
                    data={
                        "username": self.username,
                        "password": self.password,
                        "domain": self.domain,
                    },
                    follow_redirects=False,
                )
                # MeshCentral returns 302 on successful login
                cookies = dict(resp.cookies)
                for key, val in cookies.items():
                    if "meshcentral" in key.lower() or key == "connect.sid":
                        self._session_cookie = val
                        logger.debug("MeshCentral authenticated (cookie: %s)", key)
                        return True
                if resp.status_code in (200, 302) and not cookies:
                    # Some deployments embed token in response body
                    try:
                        body = resp.json()
                        token = body.get("loginToken") or body.get("token")
                        if token:
                            self._session_cookie = token
                            return True
                    except Exception:
                        pass
                logger.warning("MeshCentral login failed: status=%s", resp.status_code)
                return False
        except Exception as exc:
            logger.error("MeshCentral login error: %s", exc)
            return False

    def _headers(self) -> dict:
        h: dict = {"Content-Type": "application/json"}
        if self._session_cookie:
            h["Cookie"] = f"meshcentral.sid={self._session_cookie}"
        return h

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """Authenticated GET — re-auths once on 401."""
        if not self._session_cookie:
            if not await self._login():
                return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as client:
                resp = await client.get(
                    f"{self.url}{path}",
                    params=params,
                    headers=self._headers(),
                    follow_redirects=True,
                )
                if resp.status_code == 401:
                    self._session_cookie = None
                    if await self._login():
                        resp = await client.get(
                            f"{self.url}{path}",
                            params=params,
                            headers=self._headers(),
                            follow_redirects=True,
                        )
                if resp.status_code == 200:
                    return resp.json()
                logger.warning("MeshCentral GET %s → %s", path, resp.status_code)
                return None
        except Exception as exc:
            logger.error("MeshCentral GET %s: %s", path, exc)
            return None

    async def _post(self, path: str, payload: dict) -> Any:
        """Authenticated POST — re-auths once on 401."""
        if not self._session_cookie:
            if not await self._login():
                return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as client:
                resp = await client.post(
                    f"{self.url}{path}",
                    headers=self._headers(),
                    json=payload,
                    follow_redirects=True,
                )
                if resp.status_code == 401:
                    self._session_cookie = None
                    if await self._login():
                        resp = await client.post(
                            f"{self.url}{path}",
                            headers=self._headers(),
                            json=payload,
                            follow_redirects=True,
                        )
                if resp.status_code in (200, 201):
                    try:
                        return resp.json()
                    except Exception:
                        return {"status": "ok"}
                logger.warning("MeshCentral POST %s → %s", path, resp.status_code)
                return None
        except Exception as exc:
            logger.error("MeshCentral POST %s: %s", path, exc)
            return None

    # ── Device Inventory ──────────────────────────────────────────────────────

    async def list_devices(self) -> list[dict]:
        """Return all registered devices."""
        data = await self._get("/api/v1/devices")
        if not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("devices", data.get("nodes", []))
        return []

    async def get_device(self, node_id: str) -> dict | None:
        """Get full details for a specific device."""
        return await self._get(f"/api/v1/device/{node_id}")

    async def get_meshes(self) -> list[dict]:
        """Return all mesh groups."""
        data = await self._get("/api/v1/meshes")
        if not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("meshes", [])
        return []

    async def server_info(self) -> dict | None:
        """Return MeshCentral server metadata."""
        return await self._get("/api/v1/serverinfo")

    # ── Remote Commands ───────────────────────────────────────────────────────

    async def run_command(self, node_id: str, command: str) -> dict | None:
        """Execute a shell command on a managed device."""
        return await self._post(
            "/api/v1/runcommand",
            {"nodeids": [node_id], "cmds": command, "type": 0},
        )

    async def power_action(self, node_id: str, action: int) -> dict | None:
        """
        Send a power action to a device.
        2=sleep  4=hibernate  5=soft-off  6=hard-off  7=reset  8=soft-reset  10=wake-on-lan
        """
        return await self._post(
            "/api/v1/devicepower",
            {"nodeids": [node_id], "power": action},
        )

    async def upgrade_agent(self, node_id: str) -> dict | None:
        """Trigger a MeshCentral agent self-upgrade on a device."""
        return await self._post(
            "/api/v1/meshcommand",
            {"nodeids": [node_id], "command": 30},  # 30 = upgrade agent
        )

    # ── Agent Installation ────────────────────────────────────────────────────

    def get_agent_install_command(
        self, mesh_id: str, os_type: str = "linux"
    ) -> str:
        """
        Return the one-liner install command for a MeshCentral agent.

        mesh_id:  Target mesh (group) ID — agents connect to this mesh on install.
        os_type:  'linux' (default) or 'windows'.
        """
        if os_type.lower() == "linux":
            return (
                f'sudo sh <(curl -fsSL "{self.url}/mesh?id={mesh_id}&installflags=2") '
                f'|| wget -qO- "{self.url}/mesh?id={mesh_id}&installflags=2" | sudo bash'
            )
        return (
            f"powershell -Command \"& {{Invoke-WebRequest "
            f"-Uri '{self.url}/mesh?id={mesh_id}&installflags=2' | iex}}\""
        )

    def get_agent_install_script_url(self, mesh_id: str) -> str:
        """Return the direct URL for the Linux/amd64 agent install script."""
        return f"{self.url}/meshagents?id={mesh_id}&installflags=2&meshinstall=6"

    # ── WebSocket Event Stream ────────────────────────────────────────────────

    async def subscribe_events(
        self,
        on_event: Callable[[dict], None],
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """
        Connect to MeshCentral's WebSocket control channel and stream events.

        on_event:   Called synchronously for each normalized event dict.
        stop_event: Set this asyncio.Event to stop the subscription cleanly.

        Automatically reconnects on disconnect with exponential back-off.
        """
        try:
            import websockets
        except ImportError:
            logger.error(
                "websockets package not installed — add it to requirements.txt: "
                "websockets>=12.0"
            )
            return

        ws_url = (
            self.url.replace("https://", "wss://").replace("http://", "ws://")
            + "/control.ashx"
        )

        if not self._session_cookie:
            await self._login()

        reconnect_delay = 5

        while True:
            if stop_event and stop_event.is_set():
                break

            extra_headers: dict = {}
            if self._session_cookie:
                extra_headers["Cookie"] = f"meshcentral.sid={self._session_cookie}"

            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers=extra_headers,
                    ssl=False,
                    ping_interval=_WS_PING_INTERVAL,
                    ping_timeout=_WS_PING_TIMEOUT,
                    close_timeout=5,
                ) as ws:
                    # Authenticate over WebSocket (some deployments require this)
                    await ws.send(json.dumps({
                        "action": "login",
                        "username": self.username,
                        "password": self.password,
                        "domain": self.domain,
                    }))
                    # Subscribe to mesh + node state events
                    await ws.send(json.dumps({"action": "meshes"}))
                    await ws.send(json.dumps({"action": "nodes"}))

                    reconnect_delay = 5  # reset after successful connect
                    logger.info("MeshCentral WebSocket connected: %s", ws_url)

                    while True:
                        if stop_event and stop_event.is_set():
                            return
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                            msg = json.loads(raw)
                            event = _normalize_event(msg)
                            if event:
                                try:
                                    on_event(event)
                                except Exception as cb_exc:
                                    logger.warning("on_event callback error: %s", cb_exc)
                        except asyncio.TimeoutError:
                            await ws.send(json.dumps({"action": "ping"}))
                        except Exception as inner_exc:
                            logger.warning("MeshCentral WS recv error: %s", inner_exc)
                            break

            except Exception as exc:
                logger.warning(
                    "MeshCentral WS disconnected: %s — reconnecting in %ds",
                    exc, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)


# ── Event Normalization ───────────────────────────────────────────────────────

_ACTION_TO_TYPE: dict[str, str] = {
    "nodeconnect": "agent_connect",
    "nodedisconnect": "agent_disconnect",
    "console": "console_output",
    "node": "device_state_change",
    "event": "system_event",
    "runcommands": "command_result",
    "powerevent": "power_event",
    "agentupgrade": "agent_upgrade",
}

_SEVERITY_MAP: dict[str, str] = {
    "agent_connect": "info",
    "agent_disconnect": "medium",
    "console_output": "info",
    "device_state_change": "info",
    "system_event": "medium",
    "command_result": "info",
    "power_event": "medium",
    "agent_upgrade": "info",
}


def _normalize_event(msg: dict) -> dict | None:
    """
    Convert a raw MeshCentral WebSocket message into a normalized event dict.

    Returns None for non-actionable messages (ping, pong, serverinfo, etc.).
    """
    action = msg.get("action", "")
    if not action or action in ("ping", "pong", "serverinfo", "login"):
        return None

    event_type = _ACTION_TO_TYPE.get(action, action)
    node = msg.get("node", {})
    node_id = node.get("_id", "") or msg.get("nodeid", "") or ""
    hostname = node.get("name", "") or msg.get("name", "") or node_id
    severity = _SEVERITY_MAP.get(event_type, "info")

    return {
        "timestamp": msg.get(
            "time",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
        "event_type": event_type,
        "node_id": node_id,
        "host": hostname,
        "severity": severity,
        "details": {
            "mesh": msg.get("meshid", ""),
            "result": msg.get("result", ""),
            "output": msg.get("output", ""),
            "action": action,
        },
        "raw": msg,
    }
