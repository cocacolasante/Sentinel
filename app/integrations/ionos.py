"""
IONOS Cloud Integration — REST API v6

Manages: Datacenters (VDCs), Servers, NICs, Volumes, IPs, SSH access.

Auth: Bearer token (IONOS_TOKEN) or Basic (IONOS_USERNAME:IONOS_PASSWORD).

Docs: https://api.ionos.com/cloudapi/v6/
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_BASE = "https://api.ionos.com/cloudapi/v6"
_TIMEOUT = 60.0


def _auth_headers() -> dict:
    """Return auth headers, reading fresh settings each call to avoid stale credentials."""
    import os
    s = get_settings()
    token = s.ionos_token or os.environ.get("IONOS_TOKEN", "") or os.environ.get("IONOS_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    creds = base64.b64encode(f"{s.ionos_username}:{s.ionos_password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


class IONOSClient:
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

    async def _get(self, path: str, params: dict | None = None) -> dict:
        r = await self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict) -> dict:
        r = await self.client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    async def _post_tracked(self, path: str, body: dict) -> tuple[dict, str]:
        """POST and return (json_body, request_id) for async request tracking."""
        r = await self.client.post(path, json=body)
        r.raise_for_status()
        # IONOS returns the request ID in the Location header or X-Request-Id
        request_id = r.headers.get("X-Request-Id", "")
        location = r.headers.get("Location", "")
        if not request_id and location:
            # Location is like /requests/{id}/status
            parts = location.rstrip("/").split("/")
            if "requests" in parts:
                idx = parts.index("requests")
                if idx + 1 < len(parts):
                    request_id = parts[idx + 1]
        return r.json(), request_id

    async def _wait_for_request(
        self,
        request_id: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> dict:
        """Poll /requests/{id}/status until status is DONE or FAILED.
        Response shape: {"metadata": {"status": "DONE"|"FAILED"|"RUNNING", "message": ...}}
        """
        if not request_id:
            return {"status": "unknown", "elapsed_s": 0}
        elapsed = 0
        while elapsed < timeout:
            try:
                data = await self._get(f"/requests/{request_id}/status")
                meta = data.get("metadata", {})
                status = meta.get("status", "")
                message = meta.get("message", "")
                logger.info("Request %s status=%s elapsed=%ss", request_id, status, elapsed)
                if status in ("DONE", "FAILED"):
                    return {"status": status, "elapsed_s": elapsed, "message": message}
            except Exception as exc:
                logger.warning("Request status poll error: %s", exc)
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        return {"status": "timeout", "elapsed_s": elapsed}

    async def _put(self, path: str, body: dict) -> dict:
        r = await self.client.put(path, json=body)
        r.raise_for_status()
        return r.json()

    async def _patch(self, path: str, body: dict) -> dict:
        r = await self.client.patch(path, json=body)
        r.raise_for_status()
        return r.json()

    async def _delete(self, path: str) -> dict:
        r = await self.client.delete(path)
        r.raise_for_status()
        return {"deleted": True, "status": r.status_code}

    # ── Datacenters (VDCs) ────────────────────────────────────────────────────

    async def list_datacenters(self) -> list[dict]:
        data = await self._get("/datacenters", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "location": item["properties"].get("location", ""),
                "state": item["metadata"].get("state", ""),
            }
            for item in data.get("items", [])
        ]

    async def create_datacenter(self, name: str, location: str = "us/las", description: str = "") -> dict:
        """Create a Virtual Data Center (VDC). Locations: de/fra, de/txl, us/las, us/ewr, gb/lhr."""
        body = {
            "properties": {
                "name": name,
                "location": location,
                "description": description,
            }
        }
        result, req_id = await self._post_tracked("/datacenters", body)
        if req_id:
            await self._wait_for_request(req_id, timeout=120)
        return {
            "id": result["id"],
            "name": result["properties"].get("name"),
            "location": result["properties"].get("location"),
            "state": result["metadata"].get("state"),
        }

    async def get_datacenter(self, dc_id: str) -> dict:
        data = await self._get(f"/datacenters/{dc_id}", params={"depth": 2})
        return data

    async def delete_datacenter(self, dc_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}")

    # ── Servers ───────────────────────────────────────────────────────────────

    async def list_servers(self, dc_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/servers", params={"depth": 2})
        servers = []
        for item in data.get("items", []):
            props = item["properties"]
            servers.append(
                {
                    "id": item["id"],
                    "name": props.get("name", ""),
                    "cores": props.get("cores"),
                    "ram": props.get("ram"),
                    "state": item["metadata"].get("state", ""),
                    "vmstate": props.get("vmState", ""),
                }
            )
        return servers

    async def create_server(
        self,
        dc_id: str,
        name: str,
        cores: int = 1,
        ram_mb: int = 1024,
        cpu_family: str = "INTEL_SKYLAKE",
    ) -> dict:
        body = {
            "properties": {
                "name": name,
                "cores": cores,
                "ram": ram_mb,
                "cpuFamily": cpu_family,
            }
        }
        result = await self._post(f"/datacenters/{dc_id}/servers", body)
        return {
            "id": result["id"],
            "name": result["properties"].get("name"),
            "state": result["metadata"].get("state"),
        }

    async def start_server(self, dc_id: str, server_id: str) -> dict:
        r = await self.client.post(f"/datacenters/{dc_id}/servers/{server_id}/start")
        r.raise_for_status()
        return {"action": "start", "server_id": server_id, "status": r.status_code}

    async def stop_server(self, dc_id: str, server_id: str) -> dict:
        r = await self.client.post(f"/datacenters/{dc_id}/servers/{server_id}/stop")
        r.raise_for_status()
        return {"action": "stop", "server_id": server_id, "status": r.status_code}

    async def reboot_server(self, dc_id: str, server_id: str) -> dict:
        r = await self.client.post(f"/datacenters/{dc_id}/servers/{server_id}/reboot")
        r.raise_for_status()
        return {"action": "reboot", "server_id": server_id}

    async def delete_server(self, dc_id: str, server_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/servers/{server_id}")

    async def get_server(self, dc_id: str, server_id: str) -> dict:
        return await self._get(f"/datacenters/{dc_id}/servers/{server_id}", params={"depth": 2})

    # ── NICs (Network Interfaces) ─────────────────────────────────────────────

    async def list_nics(self, dc_id: str, server_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/servers/{server_id}/nics", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "ips": item["properties"].get("ips", []),
                "lan": item["properties"].get("lan"),
            }
            for item in data.get("items", [])
        ]

    # ── IP Blocks ─────────────────────────────────────────────────────────────

    async def list_ips(self) -> list[dict]:
        data = await self._get("/ipblocks", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "ips": item["properties"].get("ips", []),
                "location": item["properties"].get("location", ""),
                "size": item["properties"].get("size"),
            }
            for item in data.get("items", [])
        ]

    async def reserve_ip(self, location: str = "us/las", size: int = 1, name: str = "") -> dict:
        body = {"properties": {"location": location, "size": size, "name": name or f"brain-ip-{location}"}}
        result = await self._post("/ipblocks", body)
        return {"id": result["id"], "ips": result["properties"].get("ips", [])}

    # ── SSH Remote Execution ──────────────────────────────────────────────────

    def _ssh_exec_sync(
        self,
        host: str,
        command: str,
        username: str = "root",
        port: int = 22,
        timeout: int = 120,
    ) -> dict:
        """
        Execute a command on a remote server via SSH using the private key from settings.
        Returns stdout, stderr, and exit code.
        """
        import subprocess, tempfile, os

        _s = get_settings()
        # Prefer the unencrypted automation key; fall back to user key
        key_pem = _s.ionos_ssh_auto_private_key or _s.ionos_ssh_private_key
        if not key_pem:
            raise ValueError("IONOS_SSH_AUTO_PRIVATE_KEY (or IONOS_SSH_PRIVATE_KEY) not set in .env")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as tf:
            tf.write(key_pem.strip() + "\n")
            tf.flush()
            key_path = tf.name

        try:
            os.chmod(key_path, 0o600)
            cmd = [
                "ssh", "-i", key_path,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=20",
                "-o", "ServerAliveInterval=30",
                "-p", str(port),
                f"{username}@{host}",
                command,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return {
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "exit_code": result.returncode,
                "host": host,
                "command": command[:120],
            }
        finally:
            try:
                os.unlink(key_path)
            except Exception:
                pass

    async def ssh_exec(
        self,
        host: str,
        command: str,
        username: str = "root",
        port: int = 22,
        timeout: int = 120,
    ) -> dict:
        """Run a command on a remote IONOS server via SSH."""
        return await asyncio.to_thread(self._ssh_exec_sync, host, command, username, port, timeout)

    # ── Deployment helpers ────────────────────────────────────────────────────

    async def deploy_docker_app(
        self,
        host: str,
        image: str,
        container_name: str,
        port_map: str = "80:80",
        env_vars: dict | None = None,
        username: str = "root",
    ) -> dict:
        """Pull and run a Docker container on a remote IONOS server."""
        env_flags = " ".join(f"-e {k}={v}" for k, v in (env_vars or {}).items())
        cmd = (
            f"docker pull {image} && "
            f"docker rm -f {container_name} 2>/dev/null || true && "
            f"docker run -d --name {container_name} -p {port_map} {env_flags} --restart unless-stopped {image}"
        )
        return await self.ssh_exec(host, cmd, username)

    async def configure_server(
        self,
        host: str,
        commands: list[str],
        username: str = "root",
        timeout: int = 300,
    ) -> list[dict]:
        """Run a list of shell commands sequentially on a remote server."""
        results = []
        for cmd in commands:
            result = await self.ssh_exec(host, cmd, username, timeout=timeout)
            results.append(result)
            if result["exit_code"] != 0:
                logger.warning("Server config cmd failed on %s: %s", host, cmd)
        return results

    # ── Image catalogue ───────────────────────────────────────────────────────

    async def list_images(
        self,
        location: str = "",
        image_type: str = "HDD",
        name_filter: str = "",
    ) -> list[dict]:
        """
        List public IONOS images.  Optionally filter by location, type, or
        name substring (case-insensitive).
        """
        params: dict = {"depth": 1}
        if location:
            params["location"] = location
        data = await self._get("/images", params=params)
        items = data.get("items", [])
        out = []
        for item in items:
            props = item.get("properties", {})
            if props.get("imageType") != image_type:
                continue
            if props.get("public") is False:
                continue
            name = props.get("name", "")
            if name_filter and name_filter.lower() not in name.lower():
                continue
            out.append(
                {
                    "id": item["id"],
                    "name": name,
                    "location": props.get("location", ""),
                    "size_gb": props.get("size"),
                    "os_type": props.get("osType", ""),
                }
            )
        return out

    # Locations that support the CUBE server type
    CUBE_LOCATIONS: frozenset[str] = frozenset({
        "de/fra", "de/fra/2", "de/txl", "gb/lhr", "gb/bhx",
        "us/ewr", "us/mci", "fr/par", "es/vit",
    })

    async def _find_ubuntu_image(self, location: str, version: str = "22") -> str | None:
        """
        Return the image ID of the latest Ubuntu image for the given location.
        Filters strictly by props.location to avoid cross-location mismatches.
        """
        data = await self._get("/images", params={"depth": 1})
        items = data.get("items", [])

        def _match(item: dict, ver: str) -> bool:
            p = item.get("properties", {})
            return (
                p.get("public") is True
                and p.get("imageType") == "HDD"
                and p.get("location") == location
                and ver.lower() in p.get("name", "").lower()
            )

        candidates = [i for i in items if _match(i, f"ubuntu-{version}")]
        if not candidates:
            # Try any Ubuntu version in that location
            candidates = [i for i in items if _match(i, "ubuntu")]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x["properties"]["name"], reverse=True)
        return candidates[0]["id"]

    # ── CUBE server templates ─────────────────────────────────────────────────

    # Known CUBE template UUIDs (fetched 2026-03-05 from /templates endpoint)
    CUBE_TEMPLATES: dict[str, str] = {
        "Basic Cube XS":   "72e73b81-8551-4e74-b398-fc63b39994af",  # 1c 2GB 60GB
        "Basic Cube S":    "864c2c89-ea4d-4bf9-9a27-9acfeb436666",  # 2c 4GB 120GB
        "Basic Cube M":    "eda8d3b1-4d71-4502-8f22-2452adac0dc5",  # 4c 8GB 240GB
        "Basic Cube L":    "253eef63-b8b8-4c8e-9084-3ca298b3593f",  # 8c 16GB 480GB
        "Basic Cube XL":   "6382dca7-d4de-4238-b138-bdc600dc733b",  # 16c 32GB 960GB
        "Memory Cube S":   "987fb209-cc10-4acb-82d7-07833919f40f",  # 2c 8GB 120GB
        "Memory Cube M":   "e8a56b84-b717-42d9-85f6-123d12ed212e",  # 4c 16GB 240GB
        "Memory Cube L":   "639f65b6-48e5-4b12-b447-947385276a52",  # 8c 32GB 480GB
        "Memory Cube XL":  "8937cbb6-11a0-4e98-b2a5-d076cbaba3af",  # 16c 64GB 960GB
    }

    async def list_templates(self) -> list[dict]:
        """Return all available CUBE server templates with specs."""
        data = await self._get("/templates")
        out = []
        for item in data.get("items", []):
            tid = item["id"]
            detail = await self._get(f"/templates/{tid}")
            p = detail.get("properties", {})
            out.append({
                "id": tid,
                "name": p.get("name", ""),
                "cores": p.get("cores"),
                "ram_mb": p.get("ram"),
                "storage_gb": p.get("storageSize"),
                "category": p.get("category", ""),
            })
        return out

    async def _wait_for_server_available(
        self,
        dc_id: str,
        server_id: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> dict:
        """
        Poll until the server's metadata.state != 'BUSY' (i.e. 'AVAILABLE').
        Required before adding NICs to a freshly-created CUBE server.
        Returns the server status dict.
        Raises TimeoutError if the server doesn't leave BUSY within timeout seconds.
        """
        elapsed = 0
        while elapsed < timeout:
            try:
                srv = await self.get_server(dc_id, server_id)
                props = srv.get("properties", {})
                meta = srv.get("metadata", {})
                vmstate = props.get("vmState", "")
                state = meta.get("state", "")
                logger.info(
                    "Waiting for server AVAILABLE | id=%s | state=%s | vmstate=%s | elapsed=%ss",
                    server_id, state, vmstate, elapsed,
                )
                if state.upper() != "BUSY":
                    return {"vmState": vmstate, "state": state, "elapsed_s": elapsed}
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info("Server %s not yet visible (404) — still provisioning", server_id)
                else:
                    raise
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        raise TimeoutError(
            f"Server {server_id} did not leave BUSY state within {timeout}s. "
            "Check IONOS DCD for provisioning errors."
        )

    async def _wait_for_server_running(
        self,
        dc_id: str,
        server_id: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> dict:
        """
        Poll until the server's vmState == 'RUNNING' or timeout expires.
        Returns the server status dict.
        Raises TimeoutError if the server doesn't reach RUNNING within timeout seconds.
        """
        elapsed = 0
        while elapsed < timeout:
            srv = await self.get_server(dc_id, server_id)
            props = srv.get("properties", {})
            meta = srv.get("metadata", {})
            vmstate = props.get("vmState", "")
            state = meta.get("state", "")
            logger.info(
                "Waiting for server | id=%s | state=%s | vmstate=%s | elapsed=%ss",
                server_id, state, vmstate, elapsed,
            )
            if vmstate == "RUNNING":
                return {"vmState": vmstate, "state": state, "elapsed_s": elapsed}
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        raise TimeoutError(
            f"Server {server_id} did not reach RUNNING state within {timeout}s. "
            "Check IONOS DCD for provisioning errors."
        )

    # ── Full server provisioning ──────────────────────────────────────────────

    async def provision_server(
        self,
        name: str,
        location: str = "us/las",
        cores: int = 2,
        ram_mb: int = 2048,
        storage_gb: int = 20,
        ubuntu_version: str = "22",
        ssh_keys: list[str] | None = None,
        datacenter_id: str = "",
        cube_template: str = "",
        static_ip: bool = False,
        wait_for_ready: bool = False,
        wait_timeout: int = 300,
    ) -> dict:
        """
        Full Ubuntu server provisioning workflow:
          1. Create datacenter (if datacenter_id not supplied)
          2. Find the latest Ubuntu public image in the target location
          3. Optionally reserve a static IP block
          4. Create a boot volume with that image
          5. Create the server (ENTERPRISE or CUBE depending on cube_template)
          6. Attach the volume to the server
          7. Create a public LAN
          8. Create a NIC with static IP (or DHCP)
          9. Optionally wait until vmState == RUNNING

        cube_template: name like "Basic Cube M" or UUID — uses CUBE server type.
        static_ip: reserve a dedicated IP block and assign to the NIC.
        wait_for_ready: block until the server is RUNNING (up to wait_timeout seconds).
        """
        s = get_settings()
        result: dict = {"name": name, "location": location, "steps": []}

        # ── 1. Datacenter ─────────────────────────────────────────────────────
        dc_id = datacenter_id.strip()
        if not dc_id:
            dc = await self.create_datacenter(
                name=f"{name}-dc",
                location=location,
                description=f"Auto-created for server {name}",
            )
            dc_id = dc["id"]
            result["datacenter_id"] = dc_id
            result["datacenter_name"] = dc.get("name")
            result["steps"].append(f"Created datacenter {dc_id}")
            logger.info("Provisioning: datacenter created | id=%s", dc_id)
            # Poll until the DC is AVAILABLE before creating resources inside it
            for _attempt in range(30):
                await asyncio.sleep(5)
                try:
                    dc_state = await self._get(f"/datacenters/{dc_id}", params={"depth": 0})
                    if dc_state.get("metadata", {}).get("state", "").upper() == "AVAILABLE":
                        break
                except Exception:
                    pass
        else:
            result["datacenter_id"] = dc_id
            result["steps"].append(f"Using existing datacenter {dc_id}")

        # ── 2. Find Ubuntu image ──────────────────────────────────────────────
        image_id = await self._find_ubuntu_image(location, ubuntu_version)
        if not image_id:
            raise ValueError(
                f"No Ubuntu {ubuntu_version} image found in location '{location}'. "
                "Try list_images to see what is available."
            )
        result["image_id"] = image_id
        result["steps"].append(f"Found Ubuntu {ubuntu_version} image {image_id}")

        # ── 3. Reserve static IP (optional) ───────────────────────────────────
        static_ip_addr: str = ""
        if static_ip:
            ip_block = await self.reserve_ip(location=location, size=1, name=f"{name}-ip")
            ip_block_id = ip_block.get("id", "")
            ips = ip_block.get("ips", [])
            static_ip_addr = ips[0] if ips else ""
            result["ip_block_id"] = ip_block_id
            result["static_ip"] = static_ip_addr
            result["steps"].append(f"Reserved static IP: {static_ip_addr} (block {ip_block_id})")
            logger.info("Provisioning: reserved IP=%s", static_ip_addr)

        is_cube = bool(cube_template)

        # ── 4 & 5. Create server (+ DAS volume inline for CUBE) ───────────────
        if is_cube:
            # Validate CUBE location
            if location not in self.CUBE_LOCATIONS:
                raise ValueError(
                    f"Location '{location}' does not support CUBE servers. "
                    f"Supported locations: {', '.join(sorted(self.CUBE_LOCATIONS))}"
                )
            # Resolve template name → UUID
            template_uuid = self.CUBE_TEMPLATES.get(cube_template, cube_template)
            template_name = next(
                (k for k, v in self.CUBE_TEMPLATES.items() if v == template_uuid),
                cube_template,
            )
            # DAS volume properties (no size — fixed by template)
            das_vol_props: dict = {
                "name": f"{name}-boot",
                "type": "DAS",
                "image": image_id,
            }
            # Prefer the unencrypted automation key for cloud-init; fall back to user key
            _pub_key = (
                s.ionos_ssh_auto_public_key or s.ionos_ssh_public_key
            ) if not ssh_keys else ""
            if ssh_keys:
                das_vol_props["sshKeys"] = ssh_keys
            elif _pub_key:
                das_vol_props["sshKeys"] = [_pub_key]
            else:
                import secrets
                tmp_pass = secrets.token_urlsafe(16)
                das_vol_props["imagePassword"] = tmp_pass
                result["image_password"] = tmp_pass
                result["steps"].append("Note: no SSH key — image password generated (save this!)")

            # CUBE servers require a composite call: server + DAS volume in entities
            srv_body = {
                "properties": {
                    "name": name,
                    "templateUuid": template_uuid,
                    "type": "CUBE",
                },
                "entities": {
                    "volumes": {
                        "items": [{"properties": das_vol_props}]
                    }
                },
            }
            srv_raw, req_id = await self._post_tracked(f"/datacenters/{dc_id}/servers", srv_body)
            server_id = srv_raw["id"]
            # Extract the DAS volume ID from the response
            vol_items = (
                srv_raw.get("entities", {})
                .get("volumes", {})
                .get("items", [])
            )
            vol_id = vol_items[0]["id"] if vol_items else ""
            result["server_id"] = server_id
            result["volume_id"] = vol_id
            result["steps"].append(
                f"Created CUBE server {server_id} ({template_name}) with DAS boot volume"
            )
            # Wait for the async provisioning request to complete (server won't be
            # reachable via GET until the request is DONE)
            result["steps"].append("Waiting for CUBE provisioning request to complete...")
            req_status = await self._wait_for_request(req_id, timeout=300)
            result["steps"].append(
                f"Provisioning request {req_status['status']} after {req_status['elapsed_s']}s"
            )
            if req_status["status"] == "FAILED":
                raise RuntimeError(f"CUBE server provisioning failed: {req_status.get('message', '')}")
            # Extra wait for server to become AVAILABLE in case request completes before resource is ready
            result["steps"].append("Waiting for CUBE server to become AVAILABLE...")
            avail = await self._wait_for_server_available(dc_id, server_id, timeout=120)
            result["steps"].append(f"Server AVAILABLE after {avail['elapsed_s']}s (state={avail['state']})")
        else:
            # ENTERPRISE: create volume and server separately, then attach
            vol_body: dict = {
                "properties": {
                    "name": f"{name}-boot",
                    "type": "HDD",
                    "size": storage_gb,
                    "image": image_id,
                    "licenceType": "LINUX",
                }
            }
            _pub_key = (
                s.ionos_ssh_auto_public_key or s.ionos_ssh_public_key
            ) if not ssh_keys else ""
            if ssh_keys:
                vol_body["properties"]["sshKeys"] = ssh_keys
            elif _pub_key:
                vol_body["properties"]["sshKeys"] = [_pub_key]
            else:
                import secrets
                tmp_pass = secrets.token_urlsafe(16)
                vol_body["properties"]["imagePassword"] = tmp_pass
                result["image_password"] = tmp_pass
                result["steps"].append("Note: no SSH key — image password generated (save this!)")

            vol = await self._post(f"/datacenters/{dc_id}/volumes", vol_body)
            vol_id = vol["id"]
            result["volume_id"] = vol_id
            result["steps"].append(f"Created HDD boot volume {vol_id} ({storage_gb} GB)")

            srv = await self.create_server(dc_id, name, cores, ram_mb)
            server_id = srv["id"]
            result["server_id"] = server_id
            result["steps"].append(f"Created server {server_id} ({cores} cores, {ram_mb} MB RAM)")

            # Attach volume: POST .../servers/{id}/volumes  body={"id": vol_id}
            await self._post(
                f"/datacenters/{dc_id}/servers/{server_id}/volumes",
                {"id": vol_id},
            )
            result["steps"].append("Attached boot volume to server")

        # ── 7. Create LAN ─────────────────────────────────────────────────────
        lan_body = {"properties": {"name": f"{name}-public", "public": True}}
        lan = await self._post(f"/datacenters/{dc_id}/lans", lan_body)
        lan_id = lan.get("id") or lan.get("properties", {}).get("id", "1")
        result["lan_id"] = lan_id
        result["steps"].append(f"Created public LAN {lan_id}")

        # ── 8. Create NIC ─────────────────────────────────────────────────────
        # Note: do NOT set dhcp=False even when pinning a static IP.
        # IONOS uses its own DHCP to assign the reserved IP to the NIC, and the
        # cloud-init on the server uses DHCP to pick it up. Setting dhcp=False
        # breaks cloud-init networking (server becomes unreachable).
        nic_props: dict = {
            "name": f"{name}-nic",
            "lan": int(lan_id) if str(lan_id).isdigit() else 1,
        }
        if static_ip_addr:
            nic_props["ips"] = [static_ip_addr]
        # Leave dhcp unset (null) to use IONOS DHCP

        nic = await self._post(
            f"/datacenters/{dc_id}/servers/{server_id}/nics",
            {"properties": nic_props},
        )
        nic_id = nic["id"]
        result["nic_id"] = nic_id
        ip_note = f"static IP {static_ip_addr}" if static_ip_addr else "DHCP"
        result["steps"].append(f"Created NIC {nic_id} ({ip_note})")

        # ── 9. Optionally wait for RUNNING ────────────────────────────────────
        if wait_for_ready:
            result["steps"].append("Waiting for server to reach RUNNING state...")
            status = await self._wait_for_server_running(dc_id, server_id, timeout=wait_timeout)
            result["steps"].append(f"Server RUNNING after {status['elapsed_s']}s")
            result["status"] = "running"
        else:
            result["status"] = "provisioning"

        result["public_ip"] = static_ip_addr or "(DHCP — assigned within ~5 min)"
        result["note"] = (
            f"Server ready at {static_ip_addr}. SSH: ssh -i ~/.ssh/ionos_auto root@{static_ip_addr}"
            if static_ip_addr and wait_for_ready
            else "Server is provisioning. Use list_servers to check status and get IP."
        )
        logger.info(
            "Provisioning complete | server=%s | dc=%s | vol=%s | nic=%s | ip=%s",
            server_id, dc_id, vol_id, nic_id, static_ip_addr or "dhcp",
        )
        return result

    # ── Website deployment ────────────────────────────────────────────────────

    async def deploy_website(
        self,
        host: str,
        repo_url: str,
        domain: str = "",
        username: str = "root",
        branch: str = "main",
    ) -> dict:
        """
        Deploy a website on a fresh Ubuntu server via SSH.

        Detects project type:
          - Node.js/React (package.json with build script): installs Node.js, runs
            npm install + npm run build, serves the build/ directory via Apache.
          - Static/PHP: clones directly to /var/www/html.

        Apache is configured with AllowOverride All and mod_rewrite for SPA routing.
        Returns a dict with each step's exit code and output.
        """
        web_root = "/var/www/html"
        clone_dir = "/opt/app_src"

        commands = [
            # System update + Apache + git + curl
            "export DEBIAN_FRONTEND=noninteractive && apt-get update -q && apt-get install -yq apache2 git curl",
            # Clone repo into staging dir (try branch, fall back to default)
            f"rm -rf {clone_dir} && "
            f"(git clone --depth 1 --branch {branch} {repo_url} {clone_dir} 2>/dev/null || "
            f"git clone --depth 1 {repo_url} {clone_dir})",
            # Detect Node.js project and build, OR copy static files
            # If package.json + build script exists: install Node, npm install, npm build
            f"if [ -f {clone_dir}/package.json ] && grep -q '\"build\"' {clone_dir}/package.json; then "
            f"  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && "
            f"  apt-get install -yq nodejs && "
            f"  cd {clone_dir} && npm install --legacy-peer-deps && npm run build && "
            # CRA outputs to build/, Vite outputs to dist/
            f"  BUILD_DIR=$([ -d {clone_dir}/build ] && echo {clone_dir}/build || echo {clone_dir}/dist) && "
            f"  rm -rf {web_root}/* && cp -r $BUILD_DIR/. {web_root}/; "
            f"else "
            f"  rm -rf {web_root}/* && cp -r {clone_dir}/. {web_root}/; "
            f"fi",
            # Apache: AllowOverride All for .htaccess + SPA fallback
            f"cat > /etc/apache2/sites-available/000-default.conf << 'APACHEEOF'\n"
            f"<VirtualHost *:80>\n"
            f"    ServerAdmin webmaster@localhost\n"
            f"    DocumentRoot {web_root}\n"
            + (f"    ServerName {domain}\n" if domain else "")
            + f"    <Directory {web_root}>\n"
            f"        Options -Indexes +FollowSymLinks\n"
            f"        AllowOverride All\n"
            f"        Require all granted\n"
            f"    </Directory>\n"
            f"    ErrorLog ${{APACHE_LOG_DIR}}/error.log\n"
            f"    CustomLog ${{APACHE_LOG_DIR}}/access.log combined\n"
            f"</VirtualHost>\n"
            f"APACHEEOF",
            # .htaccess for React Router (SPA fallback to index.html)
            f"cat > {web_root}/.htaccess << 'HTEOF'\n"
            f"Options -MultiViews\n"
            f"RewriteEngine On\n"
            f"RewriteCond %{{REQUEST_FILENAME}} !-f\n"
            f"RewriteRule ^ index.html [QSA,L]\n"
            f"HTEOF",
            # Fix permissions + enable modules + restart
            f"chown -R www-data:www-data {web_root} && chmod -R 755 {web_root} && "
            f"a2enmod rewrite && systemctl enable apache2 && systemctl restart apache2",
            # Status check
            "systemctl is-active apache2 && echo 'Apache is running'",
            # Print public IP
            "curl -s ifconfig.me || hostname -I | awk '{print $1}'",
        ]

        results = await self.configure_server(host, commands, username, timeout=600)
        success = all(r.get("exit_code", 1) == 0 for r in results)

        # Grab public IP from last command
        public_ip = host
        last = results[-1] if results else {}
        if last.get("stdout", "").strip():
            public_ip = last["stdout"].strip().split()[0]

        return {
            "success": success,
            "host": host,
            "repo": repo_url,
            "public_ip": public_ip,
            "url": f"http://{public_ip}",
            "steps": results,
        }

    # ── Datacenter update ─────────────────────────────────────────────────────

    async def update_datacenter(self, dc_id: str, name: str = "", description: str = "") -> dict:
        body: dict = {"properties": {}}
        if name:
            body["properties"]["name"] = name
        if description:
            body["properties"]["description"] = description
        return await self._patch(f"/datacenters/{dc_id}", body)

    # ── Server extensions ──────────────────────────────────────────────────────

    async def update_server(
        self,
        dc_id: str,
        server_id: str,
        name: str = "",
        cores: int = 0,
        ram_mb: int = 0,
        cpu_family: str = "",
    ) -> dict:
        body: dict = {"properties": {}}
        if name:
            body["properties"]["name"] = name
        if cores:
            body["properties"]["cores"] = cores
        if ram_mb:
            body["properties"]["ram"] = ram_mb
        if cpu_family:
            body["properties"]["cpuFamily"] = cpu_family
        return await self._patch(f"/datacenters/{dc_id}/servers/{server_id}", body)

    async def get_server_console(self, dc_id: str, server_id: str) -> dict:
        """Get the remote console URL for a server."""
        data = await self._get(f"/datacenters/{dc_id}/servers/{server_id}/console", params={"depth": 0})
        return data

    async def suspend_server(self, dc_id: str, server_id: str) -> dict:
        r = await self.client.post(f"/datacenters/{dc_id}/servers/{server_id}/suspend")
        r.raise_for_status()
        return {"action": "suspend", "server_id": server_id, "status": r.status_code}

    # ── Volumes ───────────────────────────────────────────────────────────────

    async def list_volumes(self, dc_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/volumes", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "size_gb": item["properties"].get("size"),
                "type": item["properties"].get("type", ""),
                "state": item["metadata"].get("state", ""),
            }
            for item in data.get("items", [])
        ]

    async def get_volume(self, dc_id: str, volume_id: str) -> dict:
        return await self._get(f"/datacenters/{dc_id}/volumes/{volume_id}", params={"depth": 1})

    async def create_volume(
        self,
        dc_id: str,
        name: str,
        size_gb: int = 20,
        volume_type: str = "HDD",
        image_id: str = "",
        ssh_keys: list[str] | None = None,
        licence_type: str = "LINUX",
    ) -> dict:
        body: dict = {
            "properties": {
                "name": name,
                "type": volume_type,
                "size": size_gb,
                "licenceType": licence_type,
            }
        }
        if image_id:
            body["properties"]["image"] = image_id
        if ssh_keys:
            body["properties"]["sshKeys"] = ssh_keys
        result = await self._post(f"/datacenters/{dc_id}/volumes", body)
        return {"id": result["id"], "name": result["properties"].get("name"), "state": result["metadata"].get("state")}

    async def update_volume(self, dc_id: str, volume_id: str, name: str = "", size_gb: int = 0) -> dict:
        body: dict = {"properties": {}}
        if name:
            body["properties"]["name"] = name
        if size_gb:
            body["properties"]["size"] = size_gb
        return await self._patch(f"/datacenters/{dc_id}/volumes/{volume_id}", body)

    async def delete_volume(self, dc_id: str, volume_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/volumes/{volume_id}")

    async def list_attached_volumes(self, dc_id: str, server_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/servers/{server_id}/volumes", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "size_gb": item["properties"].get("size"),
                "type": item["properties"].get("type", ""),
            }
            for item in data.get("items", [])
        ]

    async def attach_volume(self, dc_id: str, server_id: str, volume_id: str) -> dict:
        result = await self._post(f"/datacenters/{dc_id}/servers/{server_id}/volumes/{volume_id}", {})
        return {"attached": True, "volume_id": volume_id, "server_id": server_id}

    async def detach_volume(self, dc_id: str, server_id: str, volume_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/servers/{server_id}/volumes/{volume_id}")

    async def create_volume_snapshot(self, dc_id: str, volume_id: str, name: str = "", description: str = "") -> dict:
        body: dict = {}
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        r = await self.client.post(
            f"/datacenters/{dc_id}/volumes/{volume_id}/create-snapshot",
            json=body,
        )
        r.raise_for_status()
        result = r.json()
        return {"id": result.get("id"), "name": result.get("properties", {}).get("name", name)}

    async def restore_volume_from_snapshot(self, dc_id: str, volume_id: str, snapshot_id: str) -> dict:
        body = {"id": snapshot_id}
        r = await self.client.post(
            f"/datacenters/{dc_id}/volumes/{volume_id}/restore-snapshot",
            json=body,
        )
        r.raise_for_status()
        return {"restored": True, "volume_id": volume_id, "snapshot_id": snapshot_id}

    # ── NICs (full CRUD) ──────────────────────────────────────────────────────

    async def get_nic(self, dc_id: str, server_id: str, nic_id: str) -> dict:
        return await self._get(f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}", params={"depth": 1})

    async def create_nic(
        self,
        dc_id: str,
        server_id: str,
        lan_id: int = 1,
        name: str = "",
        dhcp: bool = True,
        firewall_active: bool = False,
        ips: list[str] | None = None,
    ) -> dict:
        body: dict = {
            "properties": {
                "name": name or "nic",
                "lan": lan_id,
                "dhcp": dhcp,
                "firewallActive": firewall_active,
            }
        }
        if ips:
            body["properties"]["ips"] = ips
        result = await self._post(f"/datacenters/{dc_id}/servers/{server_id}/nics", body)
        return {
            "id": result["id"],
            "name": result["properties"].get("name"),
            "ips": result["properties"].get("ips", []),
            "lan": result["properties"].get("lan"),
        }

    async def update_nic(
        self, dc_id: str, server_id: str, nic_id: str, name: str = "", dhcp: bool | None = None, lan_id: int = 0
    ) -> dict:
        body: dict = {"properties": {}}
        if name:
            body["properties"]["name"] = name
        if dhcp is not None:
            body["properties"]["dhcp"] = dhcp
        if lan_id:
            body["properties"]["lan"] = lan_id
        return await self._patch(f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}", body)

    async def delete_nic(self, dc_id: str, server_id: str, nic_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}")

    # ── LANs ──────────────────────────────────────────────────────────────────

    async def list_lans(self, dc_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/lans", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "public": item["properties"].get("public", False),
                "state": item["metadata"].get("state", ""),
            }
            for item in data.get("items", [])
        ]

    async def get_lan(self, dc_id: str, lan_id: str) -> dict:
        return await self._get(f"/datacenters/{dc_id}/lans/{lan_id}", params={"depth": 1})

    async def create_lan(self, dc_id: str, name: str = "", public: bool = True) -> dict:
        body = {"properties": {"name": name or "lan", "public": public}}
        result = await self._post(f"/datacenters/{dc_id}/lans", body)
        return {
            "id": result.get("id"),
            "name": result.get("properties", {}).get("name"),
            "public": result.get("properties", {}).get("public"),
        }

    async def update_lan(self, dc_id: str, lan_id: str, name: str = "", public: bool | None = None) -> dict:
        body: dict = {"properties": {}}
        if name:
            body["properties"]["name"] = name
        if public is not None:
            body["properties"]["public"] = public
        return await self._patch(f"/datacenters/{dc_id}/lans/{lan_id}", body)

    async def delete_lan(self, dc_id: str, lan_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/lans/{lan_id}")

    # ── Snapshots ─────────────────────────────────────────────────────────────

    async def list_snapshots(self) -> list[dict]:
        data = await self._get("/snapshots", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "size_gb": item["properties"].get("size"),
                "location": item["properties"].get("location", ""),
                "state": item["metadata"].get("state", ""),
            }
            for item in data.get("items", [])
        ]

    async def get_snapshot(self, snapshot_id: str) -> dict:
        return await self._get(f"/snapshots/{snapshot_id}", params={"depth": 1})

    async def update_snapshot(self, snapshot_id: str, name: str = "", description: str = "") -> dict:
        body: dict = {"properties": {}}
        if name:
            body["properties"]["name"] = name
        if description:
            body["properties"]["description"] = description
        return await self._patch(f"/snapshots/{snapshot_id}", body)

    async def delete_snapshot(self, snapshot_id: str) -> dict:
        return await self._delete(f"/snapshots/{snapshot_id}")

    # ── Firewall Rules ────────────────────────────────────────────────────────

    async def list_firewall_rules(self, dc_id: str, server_id: str, nic_id: str) -> list[dict]:
        data = await self._get(
            f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}/firewallrules",
            params={"depth": 1},
        )
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "protocol": item["properties"].get("protocol", ""),
                "direction": item["properties"].get("type", ""),
            }
            for item in data.get("items", [])
        ]

    async def get_firewall_rule(self, dc_id: str, server_id: str, nic_id: str, rule_id: str) -> dict:
        return await self._get(
            f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}/firewallrules/{rule_id}",
            params={"depth": 1},
        )

    async def create_firewall_rule(
        self,
        dc_id: str,
        server_id: str,
        nic_id: str,
        name: str,
        protocol: str = "TCP",
        direction: str = "INGRESS",
        port_range_start: int = 0,
        port_range_end: int = 0,
        source_ip: str = "",
        target_ip: str = "",
    ) -> dict:
        body: dict = {
            "properties": {
                "name": name,
                "protocol": protocol,
                "type": direction,
            }
        }
        if port_range_start:
            body["properties"]["portRangeStart"] = port_range_start
        if port_range_end:
            body["properties"]["portRangeEnd"] = port_range_end
        if source_ip:
            body["properties"]["sourceIp"] = source_ip
        if target_ip:
            body["properties"]["targetIp"] = target_ip
        result = await self._post(f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}/firewallrules", body)
        return {"id": result["id"], "name": result["properties"].get("name")}

    async def delete_firewall_rule(self, dc_id: str, server_id: str, nic_id: str, rule_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/servers/{server_id}/nics/{nic_id}/firewallrules/{rule_id}")

    # ── IP Blocks ─────────────────────────────────────────────────────────────

    async def get_ip_block(self, ip_block_id: str) -> dict:
        return await self._get(f"/ipblocks/{ip_block_id}", params={"depth": 1})

    async def update_ip_block(self, ip_block_id: str, name: str) -> dict:
        body = {"properties": {"name": name}}
        return await self._patch(f"/ipblocks/{ip_block_id}", body)

    async def release_ip_block(self, ip_block_id: str) -> dict:
        return await self._delete(f"/ipblocks/{ip_block_id}")

    # ── Load Balancers ────────────────────────────────────────────────────────

    async def list_load_balancers(self, dc_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/loadbalancers", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "ip": item["properties"].get("ip", ""),
                "dhcp": item["properties"].get("dhcp", False),
            }
            for item in data.get("items", [])
        ]

    async def get_load_balancer(self, dc_id: str, lb_id: str) -> dict:
        return await self._get(f"/datacenters/{dc_id}/loadbalancers/{lb_id}", params={"depth": 2})

    async def create_load_balancer(self, dc_id: str, name: str, ip: str = "", dhcp: bool = True) -> dict:
        body: dict = {"properties": {"name": name, "dhcp": dhcp}}
        if ip:
            body["properties"]["ip"] = ip
        result = await self._post(f"/datacenters/{dc_id}/loadbalancers", body)
        return {
            "id": result["id"],
            "name": result["properties"].get("name"),
            "ip": result["properties"].get("ip", ""),
        }

    async def delete_load_balancer(self, dc_id: str, lb_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/loadbalancers/{lb_id}")

    async def list_lb_nics(self, dc_id: str, lb_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/loadbalancers/{lb_id}/balancednics", params={"depth": 1})
        return [
            {"id": item["id"], "name": item["properties"].get("name", ""), "ips": item["properties"].get("ips", [])}
            for item in data.get("items", [])
        ]

    async def add_lb_nic(self, dc_id: str, lb_id: str, nic_id: str) -> dict:
        result = await self._post(f"/datacenters/{dc_id}/loadbalancers/{lb_id}/balancednics/{nic_id}", {})
        return {"lb_id": lb_id, "nic_id": nic_id, "added": True}

    async def remove_lb_nic(self, dc_id: str, lb_id: str, nic_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/loadbalancers/{lb_id}/balancednics/{nic_id}")

    # ── NAT Gateways ──────────────────────────────────────────────────────────

    async def list_nat_gateways(self, dc_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/natgateways", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "ips": item["properties"].get("publicIps", []),
            }
            for item in data.get("items", [])
        ]

    async def get_nat_gateway(self, dc_id: str, nat_id: str) -> dict:
        return await self._get(f"/datacenters/{dc_id}/natgateways/{nat_id}", params={"depth": 2})

    async def create_nat_gateway(self, dc_id: str, name: str, public_ips: list[str]) -> dict:
        body = {"properties": {"name": name, "publicIps": public_ips}}
        result = await self._post(f"/datacenters/{dc_id}/natgateways", body)
        return {"id": result["id"], "name": result["properties"].get("name")}

    async def delete_nat_gateway(self, dc_id: str, nat_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/natgateways/{nat_id}")

    async def list_nat_rules(self, dc_id: str, nat_id: str) -> list[dict]:
        data = await self._get(f"/datacenters/{dc_id}/natgateways/{nat_id}/rules", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "type": item["properties"].get("type", ""),
                "protocol": item["properties"].get("protocol", ""),
            }
            for item in data.get("items", [])
        ]

    async def create_nat_rule(
        self,
        dc_id: str,
        nat_id: str,
        name: str,
        rule_type: str = "SNAT",
        protocol: str = "ALL",
        source_subnet: str = "0.0.0.0/0",
        public_ip: str = "",
        target_subnet: str = "",
        port_range_start: int = 0,
        port_range_end: int = 0,
    ) -> dict:
        body: dict = {
            "properties": {
                "name": name,
                "type": rule_type,
                "protocol": protocol,
                "sourceSubnet": source_subnet,
            }
        }
        if public_ip:
            body["properties"]["publicIp"] = public_ip
        if target_subnet:
            body["properties"]["targetSubnet"] = target_subnet
        if port_range_start:
            body["properties"]["targetPortRangeStart"] = port_range_start
        if port_range_end:
            body["properties"]["targetPortRangeEnd"] = port_range_end
        result = await self._post(f"/datacenters/{dc_id}/natgateways/{nat_id}/rules", body)
        return {"id": result["id"], "name": result["properties"].get("name")}

    async def delete_nat_rule(self, dc_id: str, nat_id: str, rule_id: str) -> dict:
        return await self._delete(f"/datacenters/{dc_id}/natgateways/{nat_id}/rules/{rule_id}")

    # ── Kubernetes ────────────────────────────────────────────────────────────

    async def list_k8s_clusters(self) -> list[dict]:
        data = await self._get("/k8s", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "version": item["properties"].get("k8sVersion", ""),
                "state": item["metadata"].get("state", ""),
            }
            for item in data.get("items", [])
        ]

    async def get_k8s_cluster(self, cluster_id: str) -> dict:
        return await self._get(f"/k8s/{cluster_id}", params={"depth": 2})

    async def create_k8s_cluster(
        self,
        name: str,
        k8s_version: str = "",
        maintenance_day: str = "Sunday",
        maintenance_time: str = "05:00:00",
    ) -> dict:
        body: dict = {
            "properties": {
                "name": name,
                "maintenanceWindow": {
                    "dayOfTheWeek": maintenance_day,
                    "time": maintenance_time,
                },
            }
        }
        if k8s_version:
            body["properties"]["k8sVersion"] = k8s_version
        result = await self._post("/k8s", body)
        return {
            "id": result["id"],
            "name": result["properties"].get("name"),
            "version": result["properties"].get("k8sVersion", ""),
            "state": result["metadata"].get("state", ""),
        }

    async def delete_k8s_cluster(self, cluster_id: str) -> dict:
        return await self._delete(f"/k8s/{cluster_id}")

    async def list_k8s_nodepools(self, cluster_id: str) -> list[dict]:
        data = await self._get(f"/k8s/{cluster_id}/nodepools", params={"depth": 1})
        return [
            {
                "id": item["id"],
                "name": item["properties"].get("name", ""),
                "node_count": item["properties"].get("nodeCount"),
                "state": item["metadata"].get("state", ""),
            }
            for item in data.get("items", [])
        ]

    async def get_k8s_nodepool(self, cluster_id: str, nodepool_id: str) -> dict:
        return await self._get(f"/k8s/{cluster_id}/nodepools/{nodepool_id}", params={"depth": 1})

    async def create_k8s_nodepool(
        self,
        cluster_id: str,
        name: str,
        dc_id: str,
        node_count: int = 1,
        cpu_family: str = "INTEL_SKYLAKE",
        cores: int = 2,
        ram_mb: int = 2048,
        storage_gb: int = 20,
        storage_type: str = "HDD",
        k8s_version: str = "",
    ) -> dict:
        body: dict = {
            "properties": {
                "name": name,
                "datacenterId": dc_id,
                "nodeCount": node_count,
                "cpuFamily": cpu_family,
                "coresCount": cores,
                "ramSize": ram_mb,
                "availabilityZone": "AUTO",
                "storageType": storage_type,
                "storageSize": storage_gb,
            }
        }
        if k8s_version:
            body["properties"]["k8sVersion"] = k8s_version
        result = await self._post(f"/k8s/{cluster_id}/nodepools", body)
        return {
            "id": result["id"],
            "name": result["properties"].get("name"),
            "node_count": result["properties"].get("nodeCount"),
            "state": result["metadata"].get("state", ""),
        }

    async def delete_k8s_nodepool(self, cluster_id: str, nodepool_id: str) -> dict:
        return await self._delete(f"/k8s/{cluster_id}/nodepools/{nodepool_id}")

    async def get_k8s_kubeconfig(self, cluster_id: str) -> str:
        """Return the kubeconfig YAML as a string."""
        data = await self._get(f"/k8s/{cluster_id}/kubeconfig")
        if isinstance(data, dict):
            return json.dumps(data, indent=2)
        return str(data)

    # ── Request tracking ──────────────────────────────────────────────────────

    async def get_request_status(self, request_id: str) -> dict:
        """Check the status of an async IONOS API request."""
        data = await self._get(f"/requests/{request_id}/status")
        props = data.get("metadata", {})
        return {
            "request_id": request_id,
            "status": props.get("status", data.get("status", "")),
            "message": props.get("message", ""),
        }

    # ── Single dispatch entry-point ───────────────────────────────────────────

    async def execute_action(self, action: str, params: dict) -> Any:
        """
        Unified dispatch for all IONOS DCD actions.
        Raises ValueError for unknown action names.
        """
        dc = params.get("datacenter_id", "")
        srv = params.get("server_id", "")
        vol = params.get("volume_id", "")
        nic = params.get("nic_id", "")
        lan = params.get("lan_id", "")
        snap = params.get("snapshot_id", "")
        ip_blk = params.get("ip_block_id", "")
        lb = params.get("lb_id", "")
        nat = params.get("nat_id", "")
        rule = params.get("rule_id", "")
        cluster = params.get("cluster_id", "")
        nodepool = params.get("nodepool_id", "")
        req = params.get("request_id", "")
        loc = params.get("location", "us/las")

        # ── Templates (CUBE) ──────────────────────────────────────────────────
        if action == "list_templates":
            return await self.list_templates()

        # ── Datacenters ───────────────────────────────────────────────────────
        if action == "list_datacenters":
            return await self.list_datacenters()
        if action == "get_datacenter":
            return await self.get_datacenter(dc)
        if action == "create_datacenter":
            return await self.create_datacenter(params.get("name", "brain-dc"), loc, params.get("description", ""))
        if action == "update_datacenter":
            return await self.update_datacenter(dc, params.get("name", ""), params.get("description", ""))
        if action == "delete_datacenter":
            return await self.delete_datacenter(dc)

        # ── Servers ───────────────────────────────────────────────────────────
        if action == "list_servers":
            return await self.list_servers(dc)
        if action == "get_server":
            return await self.get_server(dc, srv)
        if action == "server_status":
            data = await self.get_server(dc, srv)
            p, m = data.get("properties", {}), data.get("metadata", {})
            return {
                "id": data.get("id"),
                "name": p.get("name"),
                "cores": p.get("cores"),
                "ram_mb": p.get("ram"),
                "vmstate": p.get("vmState"),
                "state": m.get("state"),
            }
        if action == "create_server":
            return await self.create_server(
                dc,
                params.get("name", "server"),
                int(params.get("cores", 2)),
                int(params.get("ram_mb", 2048)),
                params.get("cpu_family", "INTEL_SKYLAKE"),
            )
        if action == "update_server":
            return await self.update_server(
                dc,
                srv,
                params.get("name", ""),
                int(params.get("cores", 0)),
                int(params.get("ram_mb", 0)),
                params.get("cpu_family", ""),
            )
        if action == "start_server":
            return await self.start_server(dc, srv)
        if action == "stop_server":
            return await self.stop_server(dc, srv)
        if action == "reboot_server":
            return await self.reboot_server(dc, srv)
        if action == "suspend_server":
            return await self.suspend_server(dc, srv)
        if action == "delete_server":
            return await self.delete_server(dc, srv)
        if action == "get_server_console":
            return await self.get_server_console(dc, srv)
        if action == "ssh_exec":
            return await self.ssh_exec(
                params.get("host", ""),
                params.get("command", ""),
                params.get("username", "root"),
                int(params.get("port", 22)),
            )
        if action == "deploy_docker":
            return await self.deploy_docker_app(
                params.get("host", ""),
                params.get("image", ""),
                params.get("container_name", "app"),
                params.get("port_map", "80:80"),
                params.get("env_vars"),
                params.get("username", "root"),
            )
        if action == "configure_server":
            return await self.configure_server(
                params.get("host", ""),
                params.get("commands", []),
                params.get("username", "root"),
            )
        if action == "deploy_website":
            return await self.deploy_website(
                host=params.get("host", ""),
                repo_url=params.get("repo_url", ""),
                domain=params.get("domain", ""),
                username=params.get("username", "root"),
                branch=params.get("branch", "main"),
            )
        if action == "provision_server":
            return await self.provision_server(
                name=params.get("name", "brain-server"),
                location=params.get("location", "us/las"),
                cores=int(params.get("cores", 2)),
                ram_mb=int(params.get("ram_mb", 2048)),
                storage_gb=int(params.get("storage_gb", 20)),
                ubuntu_version=str(params.get("ubuntu_version", "22")),
                ssh_keys=params.get("ssh_keys") or None,
                datacenter_id=params.get("datacenter_id", ""),
                cube_template=params.get("cube_template", ""),
                static_ip=bool(params.get("static_ip", False)),
                wait_for_ready=bool(params.get("wait_for_ready", False)),
                wait_timeout=int(params.get("wait_timeout", 300)),
            )

        # ── Volumes ───────────────────────────────────────────────────────────
        if action == "list_volumes":
            return await self.list_volumes(dc)
        if action == "get_volume":
            return await self.get_volume(dc, vol)
        if action == "create_volume":
            return await self.create_volume(
                dc,
                params.get("name", "volume"),
                int(params.get("size_gb", params.get("storage_gb", 20))),
                params.get("volume_type", "HDD"),
                params.get("image_id", ""),
                params.get("ssh_keys"),
                params.get("licence_type", "LINUX"),
            )
        if action == "update_volume":
            return await self.update_volume(dc, vol, params.get("name", ""), int(params.get("size_gb", 0)))
        if action == "delete_volume":
            return await self.delete_volume(dc, vol)
        if action == "list_attached_volumes":
            return await self.list_attached_volumes(dc, srv)
        if action == "attach_volume":
            return await self.attach_volume(dc, srv, vol)
        if action == "detach_volume":
            return await self.detach_volume(dc, srv, vol)
        if action == "create_volume_snapshot":
            return await self.create_volume_snapshot(dc, vol, params.get("name", ""), params.get("description", ""))
        if action == "restore_snapshot":
            return await self.restore_volume_from_snapshot(dc, vol, snap)

        # ── NICs ──────────────────────────────────────────────────────────────
        if action == "list_nics":
            return await self.list_nics(dc, srv)
        if action == "get_nic":
            return await self.get_nic(dc, srv, nic)
        if action == "create_nic":
            return await self.create_nic(
                dc,
                srv,
                int(params.get("lan_id", 1)),
                params.get("name", "nic"),
                bool(params.get("dhcp", True)),
                bool(params.get("firewall_active", False)),
                params.get("ips"),
            )
        if action == "update_nic":
            return await self.update_nic(
                dc,
                srv,
                nic,
                params.get("name", ""),
                params.get("dhcp"),
                int(params.get("lan_id", 0)),
            )
        if action == "delete_nic":
            return await self.delete_nic(dc, srv, nic)

        # ── LANs ──────────────────────────────────────────────────────────────
        if action == "list_lans":
            return await self.list_lans(dc)
        if action == "get_lan":
            return await self.get_lan(dc, lan)
        if action == "create_lan":
            return await self.create_lan(dc, params.get("name", "lan"), bool(params.get("public", True)))
        if action == "update_lan":
            return await self.update_lan(dc, lan, params.get("name", ""), params.get("public"))
        if action == "delete_lan":
            return await self.delete_lan(dc, lan)

        # ── Snapshots ─────────────────────────────────────────────────────────
        if action == "list_snapshots":
            return await self.list_snapshots()
        if action == "get_snapshot":
            return await self.get_snapshot(snap)
        if action == "update_snapshot":
            return await self.update_snapshot(snap, params.get("name", ""), params.get("description", ""))
        if action == "delete_snapshot":
            return await self.delete_snapshot(snap)

        # ── Firewall Rules ────────────────────────────────────────────────────
        if action == "list_firewall_rules":
            return await self.list_firewall_rules(dc, srv, nic)
        if action == "get_firewall_rule":
            return await self.get_firewall_rule(dc, srv, nic, rule)
        if action == "create_firewall_rule":
            return await self.create_firewall_rule(
                dc,
                srv,
                nic,
                params.get("name", "rule"),
                params.get("protocol", "TCP"),
                params.get("direction", "INGRESS"),
                int(params.get("port_range_start", 0)),
                int(params.get("port_range_end", 0)),
                params.get("source_ip", ""),
                params.get("target_ip", ""),
            )
        if action == "delete_firewall_rule":
            return await self.delete_firewall_rule(dc, srv, nic, rule)

        # ── IP Blocks ─────────────────────────────────────────────────────────
        if action == "list_ips":
            return await self.list_ips()
        if action == "get_ip_block":
            return await self.get_ip_block(ip_blk)
        if action == "reserve_ip":
            return await self.reserve_ip(loc, int(params.get("size", 1)), params.get("name", ""))
        if action == "update_ip_block":
            return await self.update_ip_block(ip_blk, params.get("name", ""))
        if action == "release_ip_block":
            return await self.release_ip_block(ip_blk)

        # ── Load Balancers ────────────────────────────────────────────────────
        if action == "list_load_balancers":
            return await self.list_load_balancers(dc)
        if action == "get_load_balancer":
            return await self.get_load_balancer(dc, lb)
        if action == "create_load_balancer":
            return await self.create_load_balancer(
                dc, params.get("name", "lb"), params.get("ip", ""), bool(params.get("dhcp", True))
            )
        if action == "delete_load_balancer":
            return await self.delete_load_balancer(dc, lb)
        if action == "list_lb_nics":
            return await self.list_lb_nics(dc, lb)
        if action == "add_lb_nic":
            return await self.add_lb_nic(dc, lb, nic)
        if action == "remove_lb_nic":
            return await self.remove_lb_nic(dc, lb, nic)

        # ── NAT Gateways ──────────────────────────────────────────────────────
        if action == "list_nat_gateways":
            return await self.list_nat_gateways(dc)
        if action == "get_nat_gateway":
            return await self.get_nat_gateway(dc, nat)
        if action == "create_nat_gateway":
            return await self.create_nat_gateway(dc, params.get("name", "nat"), params.get("public_ips", []))
        if action == "delete_nat_gateway":
            return await self.delete_nat_gateway(dc, nat)
        if action == "list_nat_rules":
            return await self.list_nat_rules(dc, nat)
        if action == "create_nat_rule":
            return await self.create_nat_rule(
                dc,
                nat,
                params.get("name", "rule"),
                params.get("rule_type", "SNAT"),
                params.get("protocol", "ALL"),
                params.get("source_subnet", "0.0.0.0/0"),
                params.get("public_ip", ""),
                params.get("target_subnet", ""),
                int(params.get("port_range_start", 0)),
                int(params.get("port_range_end", 0)),
            )
        if action == "delete_nat_rule":
            return await self.delete_nat_rule(dc, nat, rule)

        # ── Kubernetes ────────────────────────────────────────────────────────
        if action == "list_k8s_clusters":
            return await self.list_k8s_clusters()
        if action == "get_k8s_cluster":
            return await self.get_k8s_cluster(cluster)
        if action == "create_k8s_cluster":
            return await self.create_k8s_cluster(
                params.get("name", "k8s-cluster"),
                params.get("k8s_version", ""),
                params.get("maintenance_day", "Sunday"),
                params.get("maintenance_time", "05:00:00"),
            )
        if action == "delete_k8s_cluster":
            return await self.delete_k8s_cluster(cluster)
        if action == "list_k8s_nodepools":
            return await self.list_k8s_nodepools(cluster)
        if action == "get_k8s_nodepool":
            return await self.get_k8s_nodepool(cluster, nodepool)
        if action == "create_k8s_nodepool":
            return await self.create_k8s_nodepool(
                cluster,
                params.get("name", "nodepool"),
                dc,
                int(params.get("node_count", 1)),
                params.get("cpu_family", "INTEL_SKYLAKE"),
                int(params.get("cores", 2)),
                int(params.get("ram_mb", 2048)),
                int(params.get("storage_gb", 20)),
                params.get("storage_type", "HDD"),
                params.get("k8s_version", ""),
            )
        if action == "delete_k8s_nodepool":
            return await self.delete_k8s_nodepool(cluster, nodepool)
        if action == "get_k8s_kubeconfig":
            return await self.get_k8s_kubeconfig(cluster)

        # ── Images ────────────────────────────────────────────────────────────
        if action == "list_images":
            return await self.list_images(
                loc,
                params.get("image_type", "HDD"),
                params.get("name_filter", params.get("distro", "")),
            )

        # ── Request tracking ──────────────────────────────────────────────────
        if action == "get_request_status":
            return await self.get_request_status(req)

        raise ValueError(
            f"Unknown IONOS action: '{action}'. "
            "Use list_datacenters, list_servers, provision_server, create_server, "
            "start_server, stop_server, reboot_server, delete_server, create_datacenter, "
            "delete_datacenter, create_volume, list_volumes, attach_volume, detach_volume, "
            "create_firewall_rule, list_nics, create_nic, list_lans, create_lan, "
            "list_snapshots, create_volume_snapshot, restore_snapshot, list_images, "
            "reserve_ip, release_ip_block, list_load_balancers, create_load_balancer, "
            "list_nat_gateways, create_nat_gateway, list_k8s_clusters, create_k8s_cluster, "
            "create_k8s_nodepool, get_k8s_kubeconfig, get_request_status, ssh_exec, "
            "deploy_docker, configure_server — and many more. Ask to list all actions."
        )
