"""
RMM Skills — Remote Monitoring & Management via MeshCentral

RMMReadSkill   — device inventory, status, events, incident summary
RMMManageSkill — run commands, restart services/containers, manage agents
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)

# ── Severity badges ───────────────────────────────────────────────────────────

_SEV_BADGE = {
    "critical": "🔴",
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
    "info": "🔵",
}

_ONLINE_BADGE = {True: "🟢", False: "🔴"}


def _fmt_ts(ts) -> str:
    if not ts:
        return "N/A"
    if isinstance(ts, str):
        return ts[:16].replace("T", " ")
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)[:16]


# ── RMMReadSkill ──────────────────────────────────────────────────────────────


class RMMReadSkill(BaseSkill):
    name = "rmm_read"
    description = (
        "Read remote machine management data: list all devices, check online/offline status, "
        "view recent events, get system inventory (CPU, RAM, disk). Use when Anthony says "
        "'check server status', 'list managed devices', 'which servers are online', "
        "'show RMM inventory', or 'are any servers down'. NOT for: running commands on servers "
        "(use rmm_manage or agent_exec) or patching code (use patch_dispatch)."
    )
    trigger_intents = ["rmm_read"]
    approval_category = ApprovalCategory.NONE
    config_vars = ["MESHCENTRAL_URL", "MESHCENTRAL_USER", "MESHCENTRAL_PASSWORD"]

    def is_available(self) -> bool:
        from app.integrations.meshcentral import MeshCentralClient
        return MeshCentralClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "list")

        if action == "list":
            return await self._list_devices(params)
        if action == "get":
            return await self._get_device(params)
        if action == "status":
            return await self._status_summary(params)
        if action == "events":
            return await self._recent_events(params)
        if action == "incidents":
            return await self._incidents(params)
        if action == "inventory":
            return await self._inventory_report(params)
        if action == "meshes":
            return await self._list_meshes()

        return SkillResult(
            context_data=(
                "Unknown RMM action. Use: list, get, status, events, "
                "incidents, inventory, meshes"
            )
        )

    async def _list_devices(self, params: dict) -> SkillResult:
        from app.integrations.meshcentral import MeshCentralClient
        from app.db import postgres

        group = params.get("group", "")
        project = params.get("project", "")

        # Prefer cached DB data (faster + offline-safe)
        try:
            sql = "SELECT * FROM rmm_devices WHERE 1=1"
            args: list = []
            if group:
                sql += " AND group_name = %s"
                args.append(group)
            if project:
                sql += " AND project = %s"
                args.append(project)
            sql += " ORDER BY is_online DESC, name ASC"
            rows = postgres.execute(sql, tuple(args) if args else None)
        except Exception as e:
            logger.warning("RMMReadSkill: DB query failed in _list_devices: %s", e)
            rows = []

        if not rows:
            # Fall back to live API
            client = MeshCentralClient()
            devices = await client.list_devices()
            if not devices:
                return SkillResult(context_data="No managed devices found.")
            rows = [
                {
                    "node_id": d.get("_id", ""),
                    "name": d.get("name", "unknown"),
                    "hostname": d.get("host", ""),
                    "is_online": d.get("conn", 0) == 1,
                    "os_name": d.get("ostype", ""),
                    "group_name": "",
                    "project": "",
                    "last_seen": None,
                }
                for d in devices
            ]

        online = sum(1 for r in rows if r.get("is_online"))
        offline = len(rows) - online
        lines = [
            f"**RMM Device Inventory** ({len(rows)} total | 🟢 {online} online | 🔴 {offline} offline)\n"
        ]
        for r in rows:
            badge = _ONLINE_BADGE.get(bool(r.get("is_online")), "⚪")
            name = r.get("name") or r.get("hostname") or r.get("node_id", "?")
            grp = r.get("group_name") or ""
            proj = r.get("project") or ""
            meta = f"{grp}" + (f" / {proj}" if proj else "")
            last = _fmt_ts(r.get("last_seen"))
            lines.append(f"{badge} **{name}**{' | ' + meta if meta else ''} | last seen: {last}")
            if r.get("ip_address"):
                lines.append(f"   IP: {r['ip_address']} | OS: {r.get('os_name', 'N/A')}")
        return SkillResult(context_data="\n".join(lines))

    async def _get_device(self, params: dict) -> SkillResult:
        from app.integrations.meshcentral import MeshCentralClient
        from app.db import postgres

        node_id = params.get("node_id") or params.get("name", "")
        if not node_id:
            return SkillResult(context_data="Provide `node_id` or `name` to look up a device.")

        # Try DB first
        try:
            row = postgres.execute_one(
                "SELECT * FROM rmm_devices WHERE node_id = %s OR name ILIKE %s LIMIT 1",
                (node_id, f"%{node_id}%"),
            )
        except Exception as e:
            logger.warning("RMMReadSkill: DB query failed in _get_device: %s", e)
            row = None

        if not row:
            client = MeshCentralClient()
            row = await client.get_device(node_id)

        if not row:
            return SkillResult(context_data=f"Device `{node_id}` not found.")

        badge = _ONLINE_BADGE.get(bool(row.get("is_online")), "⚪")
        lines = [
            f"{badge} **{row.get('name', node_id)}**",
            f"Node ID: `{row.get('node_id') or row.get('_id', '?')}`",
            f"Hostname: {row.get('hostname', 'N/A')} | IP: {row.get('ip_address', 'N/A')}",
            f"OS: {row.get('os_name', 'N/A')} | Agent: {row.get('agent_version', 'N/A')}",
            f"Group: {row.get('group_name', 'N/A')} | Project: {row.get('project', 'N/A')}",
            f"Last seen: {_fmt_ts(row.get('last_seen'))}",
        ]
        if row.get("cpu_usage") is not None:
            lines.append(
                f"CPU: {row['cpu_usage']:.1f}% | RAM: {row.get('memory_usage', 0):.1f}% "
                f"| Disk: {row.get('disk_usage', 0):.1f}%"
            )
        return SkillResult(context_data="\n".join(lines))

    async def _status_summary(self, params: dict) -> SkillResult:
        from app.db import postgres

        try:
            total = postgres.execute_one("SELECT COUNT(*) AS n FROM rmm_devices")
            online_row = postgres.execute_one(
                "SELECT COUNT(*) AS n FROM rmm_devices WHERE is_online = TRUE"
            )
            recent_events = postgres.execute(
                """
                SELECT event_type, severity, COUNT(*) AS cnt
                FROM rmm_events
                WHERE created_at > NOW() - INTERVAL '1 hour'
                GROUP BY event_type, severity
                ORDER BY cnt DESC
                LIMIT 10
                """
            )
        except Exception as exc:
            return SkillResult(context_data=f"Could not load RMM status: {exc}")

        total_n = (total or {}).get("n", 0)
        online_n = (online_row or {}).get("n", 0)
        offline_n = total_n - online_n

        lines = [
            "**RMM Infrastructure Status**\n",
            f"🟢 Online: {online_n}  🔴 Offline: {offline_n}  Total: {total_n}",
            "\n**Events (last hour)**",
        ]
        if recent_events:
            for ev in recent_events:
                badge = _SEV_BADGE.get(ev.get("severity", "info"), "⚪")
                lines.append(
                    f"{badge} {ev['event_type']} × {ev['cnt']}"
                )
        else:
            lines.append("No events in the last hour.")

        return SkillResult(context_data="\n".join(lines))

    async def _recent_events(self, params: dict) -> SkillResult:
        from app.db import postgres

        limit = int(params.get("limit", 20))
        node = params.get("node_id") or params.get("name", "")
        severity = params.get("severity", "")

        try:
            sql = "SELECT * FROM rmm_events WHERE 1=1"
            args: list = []
            if node:
                sql += " AND (node_id = %s OR hostname ILIKE %s)"
                args += [node, f"%{node}%"]
            if severity:
                sql += " AND severity = %s"
                args.append(severity)
            sql += " ORDER BY created_at DESC LIMIT %s"
            args.append(limit)
            rows = postgres.execute(sql, tuple(args))
        except Exception as exc:
            return SkillResult(context_data=f"Could not load events: {exc}")

        if not rows:
            return SkillResult(context_data="No RMM events found matching that query.")

        lines = [f"**RMM Events** ({len(rows)} recent)\n"]
        for r in rows:
            badge = _SEV_BADGE.get(r.get("severity", "info"), "⚪")
            ts = _fmt_ts(r.get("created_at"))
            host = r.get("hostname") or r.get("node_id", "?")
            lines.append(
                f"{badge} `{ts}` **{r['event_type']}** — {host} | {r.get('group_name', '')}"
            )
        return SkillResult(context_data="\n".join(lines))

    async def _incidents(self, params: dict) -> SkillResult:
        from app.db import postgres

        hours = int(params.get("hours", 24))
        try:
            rows = postgres.execute(
                """
                SELECT node_id, hostname, group_name, project,
                       event_type, severity, details, created_at
                FROM rmm_events
                WHERE severity IN ('high', 'critical', 'medium')
                  AND created_at > NOW() - INTERVAL '%s hours'
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (hours,),
            )
        except Exception as exc:
            return SkillResult(context_data=f"Could not load incidents: {exc}")

        if not rows:
            return SkillResult(
                context_data=f"No incidents in the last {hours} hours. Infrastructure looks healthy."
            )

        lines = [f"**RMM Incidents** (last {hours}h | {len(rows)} events)\n"]
        for r in rows:
            badge = _SEV_BADGE.get(r.get("severity", "info"), "⚪")
            ts = _fmt_ts(r.get("created_at"))
            host = r.get("hostname") or r.get("node_id", "?")
            proj = r.get("project") or r.get("group_name") or ""
            lines.append(
                f"{badge} `{ts}` **{r['event_type']}** — {host}"
                + (f" [{proj}]" if proj else "")
            )
        return SkillResult(context_data="\n".join(lines))

    async def _inventory_report(self, params: dict) -> SkillResult:
        from app.db import postgres

        try:
            by_group = postgres.execute(
                """
                SELECT group_name, project,
                       COUNT(*) AS total,
                       SUM(CASE WHEN is_online THEN 1 ELSE 0 END) AS online
                FROM rmm_devices
                GROUP BY group_name, project
                ORDER BY group_name, project
                """
            )
        except Exception as exc:
            return SkillResult(context_data=f"Could not load inventory: {exc}")

        if not by_group:
            return SkillResult(
                context_data="No devices in inventory yet. Run an RMM full sync first."
            )

        lines = ["**RMM Infrastructure Inventory**\n"]
        for r in by_group:
            grp = r.get("group_name") or "ungrouped"
            proj = r.get("project") or ""
            label = f"{grp}" + (f" / {proj}" if proj else "")
            online = r.get("online") or 0
            total = r.get("total") or 0
            health = "🟢" if online == total else ("🟠" if online > 0 else "🔴")
            lines.append(f"{health} **{label}**: {online}/{total} online")
        return SkillResult(context_data="\n".join(lines))

    async def _list_meshes(self) -> SkillResult:
        from app.integrations.meshcentral import MeshCentralClient

        client = MeshCentralClient()
        meshes = await client.get_meshes()
        if not meshes:
            return SkillResult(context_data="No meshes found or MeshCentral unreachable.")
        lines = [f"**MeshCentral Meshes** ({len(meshes)} found)\n"]
        for m in meshes:
            mid = m.get("_id") or m.get("id", "?")
            name = m.get("name", mid)
            lines.append(f"• **{name}** — ID: `{mid}`")
        return SkillResult(context_data="\n".join(lines))


# ── RMMManageSkill ────────────────────────────────────────────────────────────


class RMMManageSkill(BaseSkill):
    name = "rmm_manage"
    description = (
        "Manage remote servers via MeshCentral RMM: run commands, restart services, reboot "
        "servers, install agents. Use when Anthony says 'restart [service] on [server]', "
        "'reboot [server]', 'run command on [server]', or 'install agent on [server]'. "
        "Requires CRITICAL approval. NOT for: reading status (use rmm_read) or code patches "
        "(use patch_dispatch)."
    )
    trigger_intents = ["rmm_manage"]
    approval_category = ApprovalCategory.CRITICAL
    config_vars = ["MESHCENTRAL_URL", "MESHCENTRAL_USER", "MESHCENTRAL_PASSWORD"]

    def is_available(self) -> bool:
        from app.integrations.meshcentral import MeshCentralClient
        return MeshCentralClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "")
        node_id = params.get("node_id") or params.get("name", "")

        if not action:
            return SkillResult(
                context_data=(
                    "Specify an action: run_command, restart_service, restart_container, "
                    "reboot, upgrade_agent, install_agent"
                )
            )

        # install_agent doesn't require a node_id (it installs on a new host)
        if action != "install_agent" and not node_id:
            return SkillResult(context_data="Provide `node_id` or `name` of the target device.")

        # Downgrade category for read-ish actions
        if action == "upgrade_agent":
            self.approval_category = ApprovalCategory.STANDARD

        label = _build_label(action, params)
        return SkillResult(
            context_data=label,
            pending_action={
                "action": f"rmm_{action}",
                "params": params,
                "original": original_message,
            },
        )


def _build_label(action: str, params: dict) -> str:
    node = params.get("node_id") or params.get("name", "?")
    if action == "run_command":
        cmd = params.get("command", "")[:80]
        return f"Run command on `{node}`: `{cmd}`"
    if action == "restart_service":
        svc = params.get("service", "?")
        return f"Restart service `{svc}` on `{node}`"
    if action == "restart_container":
        ctr = params.get("container", "?")
        return f"Restart container `{ctr}` on `{node}`"
    if action == "reboot":
        return f"Reboot server `{node}`"
    if action == "upgrade_agent":
        return f"Upgrade MeshCentral agent on `{node}`"
    if action == "install_agent":
        host = params.get("host", "?")
        mesh = params.get("mesh_id", "default mesh")
        return f"Install MeshCentral agent on `{host}` → mesh `{mesh}`"
    return f"RMM action `{action}` on `{node}`"
