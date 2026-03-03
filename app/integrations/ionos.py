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

logger   = logging.getLogger(__name__)
settings = get_settings()

_BASE    = "https://api.ionos.com/cloudapi/v6"
_TIMEOUT = 60.0


def _auth_headers() -> dict:
    if settings.ionos_token:
        return {"Authorization": f"Bearer {settings.ionos_token}"}
    creds = base64.b64encode(
        f"{settings.ionos_username}:{settings.ionos_password}".encode()
    ).decode()
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
                "id":       item["id"],
                "name":     item["properties"].get("name", ""),
                "location": item["properties"].get("location", ""),
                "state":    item["metadata"].get("state", ""),
            }
            for item in data.get("items", [])
        ]

    async def create_datacenter(self, name: str, location: str = "us/las", description: str = "") -> dict:
        """Create a Virtual Data Center (VDC). Locations: de/fra, de/txl, us/las, us/ewr, gb/lhr."""
        body = {
            "properties": {
                "name":        name,
                "location":    location,
                "description": description,
            }
        }
        result = await self._post("/datacenters", body)
        return {
            "id":       result["id"],
            "name":     result["properties"].get("name"),
            "location": result["properties"].get("location"),
            "state":    result["metadata"].get("state"),
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
            servers.append({
                "id":     item["id"],
                "name":   props.get("name", ""),
                "cores":  props.get("cores"),
                "ram":    props.get("ram"),
                "state":  item["metadata"].get("state", ""),
                "vmstate": props.get("vmState", ""),
            })
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
                "name":      name,
                "cores":     cores,
                "ram":       ram_mb,
                "cpuFamily": cpu_family,
            }
        }
        result = await self._post(f"/datacenters/{dc_id}/servers", body)
        return {
            "id":    result["id"],
            "name":  result["properties"].get("name"),
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
                "id":   item["id"],
                "name": item["properties"].get("name", ""),
                "ips":  item["properties"].get("ips", []),
                "lan":  item["properties"].get("lan"),
            }
            for item in data.get("items", [])
        ]

    # ── IP Blocks ─────────────────────────────────────────────────────────────

    async def list_ips(self) -> list[dict]:
        data = await self._get("/ipblocks", params={"depth": 1})
        return [
            {
                "id":       item["id"],
                "ips":      item["properties"].get("ips", []),
                "location": item["properties"].get("location", ""),
                "size":     item["properties"].get("size"),
            }
            for item in data.get("items", [])
        ]

    async def reserve_ip(self, location: str = "us/las", size: int = 1, name: str = "") -> dict:
        body = {"properties": {"location": location, "size": size, "name": name or f"brain-ip-{location}"}}
        result = await self._post("/ipblocks", body)
        return {"id": result["id"], "ips": result["properties"].get("ips", [])}

    # ── SSH Remote Execution ──────────────────────────────────────────────────

    def _ssh_exec_sync(self, host: str, command: str, username: str = "root", port: int = 22) -> dict:
        """
        Execute a command on a remote server via SSH using the private key from settings.
        Returns stdout, stderr, and exit code.
        """
        import subprocess, tempfile, os

        key_pem = settings.ionos_ssh_private_key
        if not key_pem:
            raise ValueError("IONOS_SSH_PRIVATE_KEY not set in .env")

        # Write key to temp file (paramiko-style, but using subprocess SSH)
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
                "-o", "ConnectTimeout=15",
                "-p", str(port),
                f"{username}@{host}",
                command,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return {
                "stdout":    result.stdout.strip(),
                "stderr":    result.stderr.strip(),
                "exit_code": result.returncode,
                "host":      host,
            }
        finally:
            try:
                os.unlink(key_path)
            except Exception:
                pass

    async def ssh_exec(self, host: str, command: str, username: str = "root", port: int = 22) -> dict:
        """Run a command on a remote IONOS server via SSH."""
        return await asyncio.to_thread(self._ssh_exec_sync, host, command, username, port)

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

    async def configure_server(self, host: str, commands: list[str], username: str = "root") -> list[dict]:
        """Run a list of shell commands sequentially on a remote server."""
        results = []
        for cmd in commands:
            result = await self.ssh_exec(host, cmd, username)
            results.append(result)
            if result["exit_code"] != 0:
                logger.warning("Server config cmd failed on {}: {}", host, cmd)
        return results
