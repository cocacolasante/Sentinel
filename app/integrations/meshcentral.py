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
        self.public_url = s.meshcentral_url.rstrip("/")
        # Use internal URL for API calls if configured, otherwise fall back to public URL
        self.url = (s.meshcentral_internal_url or s.meshcentral_url).rstrip("/")
        self.username = s.meshcentral_user
        self.password = s.meshcentral_password
        self.domain = s.meshcentral_domain or ""
        self._session_cookie: Optional[str] = None
        self._session_cookie_name: str = ""

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
                # Preferred cookie names (in priority order)
                _PREFERRED = ("meshcentral.sid", "connect.sid", "xid")
                for key in _PREFERRED:
                    if key in cookies:
                        self._session_cookie = cookies[key]
                        self._session_cookie_name = key
                        logger.debug("MeshCentral authenticated (cookie: %s)", key)
                        return True
                # Fallback: any cookie set on successful response
                if resp.status_code in (200, 302) and cookies:
                    key, val = next(iter(cookies.items()))
                    self._session_cookie = val
                    self._session_cookie_name = key
                    logger.debug("MeshCentral authenticated (cookie: %s)", key)
                    return True
                if resp.status_code in (200, 302) and not cookies:
                    # Some deployments embed token in response body
                    try:
                        body = resp.json()
                        token = body.get("loginToken") or body.get("token")
                        if token:
                            self._session_cookie = token
                            self._session_cookie_name = "loginToken"
                            return True
                    except Exception:
                        pass
                logger.warning("MeshCentral login failed: status=%s", resp.status_code)
                return False
        except Exception as exc:
            logger.error("MeshCentral login error: %s", exc)
            return False

    def _ws_url(self) -> str:
        """WebSocket control URL derived from the internal API URL."""
        return (
            self.url.replace("https://", "wss://").replace("http://", "ws://")
            + "/control.ashx"
        )

    async def _get_cookie_header(self) -> str:
        """HTTP login to get session cookies for WebSocket auth."""
        http_url = self.url.replace("ws://", "http://").replace("wss://", "https://")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as c:
                r = await c.post(
                    f"{http_url}/login",
                    data={"username": self.username, "password": self.password, "domain": self.domain},
                    follow_redirects=False,
                )
                cookies = dict(r.cookies)
                parts = [f"{k}={v}" for k, v in cookies.items()]
                return "; ".join(parts)
        except Exception as exc:
            logger.error("MeshCentral cookie login failed: %s", exc)
            return ""

    async def _ws_query(
        self,
        send_actions: list[dict],
        collect_actions: set[str],
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """
        One-shot WebSocket query: authenticate via cookie, send actions, collect responses.

        send_actions:    List of action dicts to send after connection.
        collect_actions: Set of action names to collect before closing.
        Returns a dict keyed by action name → response payload.
        """
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed")
            return {}

        results: dict[str, Any] = {}
        ws_url = self._ws_url()
        ssl_ctx: Any = False if ws_url.startswith("wss://") else None

        # Auth via HTTP session cookie
        cookie_hdr = await self._get_cookie_header()
        extra_headers: dict = {}
        if cookie_hdr:
            extra_headers["Cookie"] = cookie_hdr

        # WS server sends serverinfo, userinfo etc. on connect — we collect after those
        _SKIP_ACTIONS = {"serverinfo", "userinfo", "traceinfo", "close"}

        try:
            async with websockets.connect(
                ws_url,
                additional_headers=extra_headers,
                ssl=ssl_ctx,
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                # Send all requested actions after connecting
                for action in send_actions:
                    await ws.send(json.dumps(action))

                # Collect expected responses
                deadline = asyncio.get_event_loop().time() + timeout
                remaining = set(collect_actions)
                while remaining:
                    wait = deadline - asyncio.get_event_loop().time()
                    if wait <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                        msg = json.loads(raw)
                        action_name = msg.get("action", "")
                        if action_name == "close":
                            logger.warning("MeshCentral WS closed: %s", msg.get("msg", ""))
                            break
                        if action_name in remaining:
                            results[action_name] = msg
                            remaining.discard(action_name)
                    except asyncio.TimeoutError:
                        break
                    except Exception as exc:
                        logger.warning("WS recv error: %s", exc)
                        break

        except Exception as exc:
            logger.error("MeshCentral WS query failed: %s", exc)

        return results

    # ── Device Inventory ──────────────────────────────────────────────────────

    async def list_devices(self) -> list[dict]:
        """Return all registered devices via WebSocket API."""
        results = await self._ws_query(
            send_actions=[{"action": "nodes"}, {"action": "meshes"}],
            collect_actions={"nodes", "meshes"},
        )

        # Build meshid → mesh name lookup from meshes response
        mesh_name: dict[str, str] = {}
        meshes_data = results.get("meshes", {}).get("meshes", [])
        if isinstance(meshes_data, list):
            for m in meshes_data:
                if isinstance(m, dict):
                    mesh_name[m.get("_id", "")] = m.get("name", "")
        elif isinstance(meshes_data, dict):
            for mid, m in meshes_data.items():
                mesh_name[mid] = m.get("name", "") if isinstance(m, dict) else ""

        nodes_map = results.get("nodes", {}).get("nodes", {})
        devices: list[dict] = []
        if isinstance(nodes_map, dict):
            # {"meshid": [node,...], ...}  OR  {"meshid": {"nodeid": node,...}, ...}
            for mesh_id, mesh_nodes in nodes_map.items():
                node_list: list[dict] = []
                if isinstance(mesh_nodes, list):
                    node_list = [n for n in mesh_nodes if isinstance(n, dict)]
                elif isinstance(mesh_nodes, dict):
                    node_list = [n for n in mesh_nodes.values() if isinstance(n, dict)]
                # Inject meshid and groupname into each node
                for node in node_list:
                    node["meshid"] = mesh_id
                    node["groupname"] = mesh_name.get(mesh_id, "")
                devices.extend(node_list)
        elif isinstance(nodes_map, list):
            devices = [n for n in nodes_map if isinstance(n, dict)]
        return devices

    async def get_device(self, node_id: str) -> dict | None:
        """Get details for a specific device via WebSocket."""
        devices = await self.list_devices()
        for dev in devices:
            dev_id = dev.get("_id") or dev.get("id", "")
            if dev_id == node_id:
                return dev
        return None

    async def get_meshes(self) -> list[dict]:
        """Return all mesh groups via WebSocket API."""
        results = await self._ws_query(
            send_actions=[{"action": "meshes"}],
            collect_actions={"meshes"},
        )
        meshes_msg = results.get("meshes", {})
        meshes = meshes_msg.get("meshes", {})
        if isinstance(meshes, dict):
            return list(meshes.values())
        if isinstance(meshes, list):
            return meshes
        return []

    async def server_info(self) -> dict | None:
        """Return MeshCentral server metadata via WebSocket."""
        results = await self._ws_query(
            send_actions=[],
            collect_actions={"serverinfo"},
            timeout=8.0,
        )
        return results.get("serverinfo")

    # ── Remote Commands ───────────────────────────────────────────────────────

    async def run_command(self, node_id: str, command: str) -> dict | None:
        """Execute a shell command on a managed device."""
        results = await self._ws_query(
            send_actions=[{
                "action": "runcommands",
                "nodeids": [node_id],
                "cmds": command,
                "type": 0,
                "rights": 4,
                "responseid": "run1",
            }],
            collect_actions={"runcommands"},
        )
        return results.get("runcommands")

    async def power_action(self, node_id: str, action: int) -> dict | None:
        """
        Send a power action to a device.
        2=sleep  4=hibernate  5=soft-off  6=hard-off  7=reset  8=soft-reset  10=wake-on-lan
        """
        results = await self._ws_query(
            send_actions=[{
                "action": "poweraction",
                "nodeids": [node_id],
                "actiontype": action,
            }],
            collect_actions={"poweraction"},
        )
        return results.get("poweraction", {"status": "sent"})

    async def upgrade_agent(self, node_id: str) -> dict | None:
        """Trigger a MeshCentral agent self-upgrade on a device."""
        results = await self._ws_query(
            send_actions=[{
                "action": "meshcommand",
                "nodeids": [node_id],
                "command": 30,
            }],
            collect_actions={"meshcommand"},
        )
        return results.get("meshcommand", {"status": "sent"})

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

        ws_url = self._ws_url()

        if not self._session_cookie:
            await self._login()

        reconnect_delay = 5

        while True:
            if stop_event and stop_event.is_set():
                break

            extra_headers: dict = {}
            if self._session_cookie:
                extra_headers["Cookie"] = f"meshcentral.sid={self._session_cookie}"

            ssl_ctx = False if ws_url.startswith("wss://") else None
            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers=extra_headers,
                    ssl=ssl_ctx,
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
