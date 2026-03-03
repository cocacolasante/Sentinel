"""
IONOS skills — manage cloud infrastructure and DNS.

Intents:
  ionos_cloud — manage datacenters, servers (create, start, stop, SSH, deploy)
  ionos_dns   — manage DNS zones and records
"""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class IONOSCloudSkill(BaseSkill):
    name = "ionos_cloud"
    description = (
        "Manage IONOS cloud: list/create datacenters (VDCs), spin servers up/down, "
        "SSH into servers, deploy applications, configure infrastructure"
    )
    trigger_intents = ["ionos_cloud"]
    requires_confirmation = True
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.ionos import IONOSClient
        return IONOSClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.ionos import IONOSClient
        client = IONOSClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[IONOS not configured — set IONOS_TOKEN or IONOS_USERNAME/IONOS_PASSWORD in .env]",
                skill_name=self.name,
            )

        action    = params.get("action", "list_datacenters")
        dc_id     = params.get("datacenter_id", "")
        server_id = params.get("server_id", "")

        # ── Read-only actions (no confirmation needed) ────────────────────────
        if action == "list_datacenters":
            data = await client.list_datacenters()
            return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)

        if action == "list_servers":
            if not dc_id:
                return SkillResult(context_data="[list_servers requires datacenter_id]", skill_name=self.name)
            data = await client.list_servers(dc_id)
            return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)

        if action == "list_ips":
            data = await client.list_ips()
            return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)

        if action == "server_status":
            if not dc_id or not server_id:
                return SkillResult(context_data="[server_status requires datacenter_id and server_id]", skill_name=self.name)
            data = await client.get_server(dc_id, server_id)
            props = data.get("properties", {})
            meta  = data.get("metadata", {})
            summary = {
                "id":      data.get("id"),
                "name":    props.get("name"),
                "cores":   props.get("cores"),
                "ram_mb":  props.get("ram"),
                "vmstate": props.get("vmState"),
                "state":   meta.get("state"),
            }
            return SkillResult(context_data=json.dumps(summary, indent=2), skill_name=self.name)

        if action == "ssh_exec":
            # Read-only SSH (status check, etc.) — still needs confirmation as it runs remote code
            host    = params.get("host", "")
            command = params.get("command", "")
            if not host or not command:
                return SkillResult(context_data="[ssh_exec requires host and command]", skill_name=self.name)

        # ── Write / destructive actions — route through confirmation ──────────
        action_descriptions = {
            "create_datacenter": f"Create datacenter: **{params.get('name', '?')}** in `{params.get('location', 'us/las')}`",
            "create_server":     f"Create server: **{params.get('name', '?')}** ({params.get('cores', 1)} cores, {params.get('ram_mb', 1024)} MB RAM) in datacenter `{dc_id}`",
            "start_server":      f"Start server `{server_id}` in datacenter `{dc_id}`",
            "stop_server":       f"Stop server `{server_id}` in datacenter `{dc_id}`",
            "reboot_server":     f"Reboot server `{server_id}` in datacenter `{dc_id}`",
            "delete_server":     f"**DELETE** server `{server_id}` in datacenter `{dc_id}` — this cannot be undone",
            "delete_datacenter": f"**DELETE** entire datacenter `{dc_id}` — this cannot be undone",
            "ssh_exec":          f"Run `{params.get('command', '?')}` on `{params.get('host', '?')}`",
            "deploy_docker":     f"Deploy **{params.get('image', '?')}** as `{params.get('container_name', '?')}` on `{params.get('host', '?')}`",
            "configure_server":  f"Run {len(params.get('commands', []))} config command(s) on `{params.get('host', '?')}`",
            "reserve_ip":        f"Reserve {params.get('size', 1)} IP(s) in `{params.get('location', 'us/las')}`",
        }

        # delete_server / delete_datacenter → BREAKING
        if action in ("delete_server", "delete_datacenter"):
            self.__class__.approval_category = ApprovalCategory.BREAKING

        description = action_descriptions.get(action, f"IONOS action: {action}")
        pending = {
            "intent":   "ionos_cloud",
            "action":   action,
            "params":   params,
            "original": original_message,
        }
        context = (
            f"Show the user the following IONOS cloud action and ask for confirmation:\n\n"
            f"**{description}**\n\n"
            "Reply **confirm** to proceed or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )


class IONOSDNSSkill(BaseSkill):
    name = "ionos_dns"
    description = "Manage IONOS DNS: list zones, create/update/delete DNS records (A, CNAME, MX, TXT, etc.)"
    trigger_intents = ["ionos_dns"]
    requires_confirmation = True
    approval_category = ApprovalCategory.CRITICAL

    def is_available(self) -> bool:
        from app.integrations.ionos_dns import IONOSDNSClient
        return IONOSDNSClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.ionos_dns import IONOSDNSClient
        client = IONOSDNSClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[IONOS not configured — set IONOS_TOKEN in .env]",
                skill_name=self.name,
            )

        action  = params.get("action", "list_zones")
        zone_id = params.get("zone_id", "")

        # Read-only
        if action == "list_zones":
            data = await client.list_zones()
            return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)

        if action == "list_records":
            if not zone_id:
                return SkillResult(context_data="[list_records requires zone_id]", skill_name=self.name)
            data = await client.list_records(zone_id)
            return SkillResult(context_data=json.dumps(data, indent=2), skill_name=self.name)

        # Write actions — confirmation
        pending = {
            "intent":   "ionos_dns",
            "action":   action,
            "params":   params,
            "original": original_message,
        }
        action_descriptions = {
            "create_record": (
                f"Create {params.get('type', '?')} record `{params.get('name', '?')}` → "
                f"`{params.get('content', '?')}` (TTL {params.get('ttl', 3600)}) "
                f"in zone `{params.get('zone_name', zone_id)}`"
            ),
            "update_record": f"Update record `{params.get('record_id', '?')}` in zone `{zone_id}`",
            "delete_record": f"**DELETE** record `{params.get('record_id', '?')}` from zone `{zone_id}`",
            "upsert_record": (
                f"Upsert {params.get('type', '?')} record `{params.get('name', '?')}` → "
                f"`{params.get('content', '?')}` in zone `{params.get('zone_name', '?')}`"
            ),
            "create_zone":   f"Create DNS zone `{params.get('zone_name', '?')}`",
        }
        description = action_descriptions.get(action, f"DNS action: {action}")
        context = (
            f"Show the user this DNS change and ask for confirmation:\n\n"
            f"**{description}**\n\n"
            "Reply **confirm** to proceed or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
