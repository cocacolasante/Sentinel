"""
IONOS skills — manage cloud infrastructure and DNS.

Intents:
  ionos_cloud — manage datacenters, servers, volumes, NICs, LANs, snapshots,
                firewall rules, load balancers, NAT gateways, Kubernetes, SSH
  ionos_dns   — manage DNS zones and records
"""

from __future__ import annotations

import json
import logging

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)

# ── Action classification ─────────────────────────────────────────────────────

# Read-only: execute immediately, no confirmation
_READ_ACTIONS: frozenset[str] = frozenset(
    {
        "list_datacenters",
        "get_datacenter",
        "list_servers",
        "get_server",
        "server_status",
        "list_volumes",
        "get_volume",
        "list_attached_volumes",
        "list_snapshots",
        "get_snapshot",
        "list_nics",
        "get_nic",
        "list_lans",
        "get_lan",
        "list_ips",
        "get_ip_block",
        "list_images",
        "list_firewall_rules",
        "get_firewall_rule",
        "list_load_balancers",
        "get_load_balancer",
        "list_lb_nics",
        "list_nat_gateways",
        "get_nat_gateway",
        "list_nat_rules",
        "list_k8s_clusters",
        "get_k8s_cluster",
        "list_k8s_nodepools",
        "get_k8s_nodepool",
        "get_k8s_kubeconfig",
        "get_request_status",
        "get_server_console",
        "list_templates",
    }
)

# Write actions that require CRITICAL confirmation
_CONFIRM_ACTIONS: frozenset[str] = frozenset(
    {
        "create_datacenter",
        "update_datacenter",
        "create_server",
        "update_server",
        "start_server",
        "stop_server",
        "reboot_server",
        "suspend_server",
        "provision_server",
        "create_volume",
        "update_volume",
        "attach_volume",
        "detach_volume",
        "create_volume_snapshot",
        "restore_snapshot",
        "create_nic",
        "update_nic",
        "create_lan",
        "update_lan",
        "update_snapshot",
        "create_firewall_rule",
        "reserve_ip",
        "update_ip_block",
        "create_load_balancer",
        "add_lb_nic",
        "remove_lb_nic",
        "create_nat_gateway",
        "create_nat_rule",
        "create_k8s_cluster",
        "create_k8s_nodepool",
        "ssh_exec",
        "deploy_docker",
        "configure_server",
        "deploy_website",
    }
)

# Destructive/irreversible: require BREAKING confirmation
_BREAKING_ACTIONS: frozenset[str] = frozenset(
    {
        "delete_datacenter",
        "delete_server",
        "delete_volume",
        "delete_snapshot",
        "delete_nic",
        "delete_lan",
        "release_ip_block",
        "delete_firewall_rule",
        "delete_load_balancer",
        "delete_nat_gateway",
        "delete_nat_rule",
        "delete_k8s_cluster",
        "delete_k8s_nodepool",
    }
)


def _describe_action(action: str, params: dict) -> str:
    """Return a human-readable one-liner for the confirmation prompt."""
    dc = params.get("datacenter_id", "")
    srv = params.get("server_id", "")
    loc = params.get("location", "us/las")
    _dc_ctx = f"existing DC `{dc}`" if dc else f"new DC in `{loc}`"

    descs = {
        # Datacenters
        "create_datacenter": f"Create datacenter **{params.get('name', '?')}** in `{loc}`",
        "update_datacenter": f"Update datacenter `{dc}`",
        "delete_datacenter": f"**DELETE** entire datacenter `{dc}` — irreversible",
        # Servers
        "provision_server": (
            f"Provision Ubuntu {params.get('ubuntu_version', '22')} server "
            f"**{params.get('name', 'brain-server')}** "
            + (
                f"(CUBE: {params.get('cube_template', '')}) "
                if params.get("cube_template") else
                f"({params.get('cores', 2)} cores, {params.get('ram_mb', 2048)} MB RAM, {params.get('storage_gb', 20)} GB) "
            )
            + f"in {_dc_ctx}"
            + (" with **static IP**" if params.get("static_ip") else "")
            + (" — will wait for RUNNING state" if params.get("wait_for_ready") else "")
            + ". Steps: DC → image → IP reservation → volume → server → LAN → NIC."
        ),
        "deploy_website": (
            f"Deploy **{params.get('repo_url', '?')}** on server `{params.get('host', '?')}` "
            f"via Apache2"
            + (f" (domain: {params.get('domain')})" if params.get("domain") else "")
            + ". Steps: apt install apache2+git → clone repo → set permissions → restart Apache."
        ),
        "create_server": f"Create server **{params.get('name', '?')}** ({params.get('cores', 2)} cores, {params.get('ram_mb', 2048)} MB RAM) in `{dc}`",
        "update_server": f"Update server `{srv}` in `{dc}`",
        "start_server": f"Start server `{srv}` in `{dc}`",
        "stop_server": f"Stop server `{srv}` in `{dc}`",
        "reboot_server": f"Reboot server `{srv}` in `{dc}`",
        "suspend_server": f"Suspend server `{srv}` in `{dc}`",
        "delete_server": f"**DELETE** server `{srv}` in `{dc}` — irreversible",
        "ssh_exec": f"Run `{params.get('command', '?')}` on `{params.get('host', '?')}`",
        "deploy_docker": f"Deploy **{params.get('image', '?')}** as `{params.get('container_name', '?')}` on `{params.get('host', '?')}`",
        "configure_server": f"Run {len(params.get('commands', []))} command(s) on `{params.get('host', '?')}`",
        # Volumes
        "create_volume": f"Create {params.get('volume_type', 'HDD')} volume **{params.get('name', '?')}** ({params.get('size_gb', 20)} GB) in `{dc}`",
        "update_volume": f"Update volume `{params.get('volume_id', '?')}` in `{dc}`",
        "attach_volume": f"Attach volume `{params.get('volume_id', '?')}` to server `{srv}` in `{dc}`",
        "detach_volume": f"Detach volume `{params.get('volume_id', '?')}` from server `{srv}` in `{dc}`",
        "delete_volume": f"**DELETE** volume `{params.get('volume_id', '?')}` in `{dc}` — irreversible",
        "create_volume_snapshot": f"Snapshot volume `{params.get('volume_id', '?')}` in `{dc}` as **{params.get('name', 'snapshot')}**",
        "restore_snapshot": f"Restore snapshot `{params.get('snapshot_id', '?')}` onto volume `{params.get('volume_id', '?')}` in `{dc}`",
        # NICs
        "create_nic": f"Create NIC on server `{srv}` in `{dc}` (LAN {params.get('lan_id', 1)}, DHCP {params.get('dhcp', True)})",
        "update_nic": f"Update NIC `{params.get('nic_id', '?')}` on server `{srv}` in `{dc}`",
        "delete_nic": f"**DELETE** NIC `{params.get('nic_id', '?')}` from server `{srv}` in `{dc}`",
        # LANs
        "create_lan": f"Create {'public' if params.get('public', True) else 'private'} LAN **{params.get('name', '?')}** in `{dc}`",
        "update_lan": f"Update LAN `{params.get('lan_id', '?')}` in `{dc}`",
        "delete_lan": f"**DELETE** LAN `{params.get('lan_id', '?')}` in `{dc}`",
        # Snapshots
        "update_snapshot": f"Update snapshot `{params.get('snapshot_id', '?')}`",
        "delete_snapshot": f"**DELETE** snapshot `{params.get('snapshot_id', '?')}` — irreversible",
        # Firewall
        "create_firewall_rule": f"Create {params.get('protocol', 'TCP')} {params.get('direction', 'INGRESS')} rule **{params.get('name', '?')}** on NIC `{params.get('nic_id', '?')}`",
        "delete_firewall_rule": f"**DELETE** firewall rule `{params.get('rule_id', '?')}` — irreversible",
        # IPs
        "reserve_ip": f"Reserve {params.get('size', 1)} IP(s) in `{loc}`",
        "update_ip_block": f"Rename IP block `{params.get('ip_block_id', '?')}`",
        "release_ip_block": f"**RELEASE** IP block `{params.get('ip_block_id', '?')}` — irreversible",
        # Load Balancers
        "create_load_balancer": f"Create load balancer **{params.get('name', '?')}** in `{dc}`",
        "delete_load_balancer": f"**DELETE** load balancer `{params.get('lb_id', '?')}` in `{dc}` — irreversible",
        "add_lb_nic": f"Balance NIC `{params.get('nic_id', '?')}` under LB `{params.get('lb_id', '?')}` in `{dc}`",
        "remove_lb_nic": f"Remove NIC `{params.get('nic_id', '?')}` from LB `{params.get('lb_id', '?')}` in `{dc}`",
        # NAT Gateways
        "create_nat_gateway": f"Create NAT gateway **{params.get('name', '?')}** in `{dc}` with IPs {params.get('public_ips', [])}",
        "delete_nat_gateway": f"**DELETE** NAT gateway `{params.get('nat_id', '?')}` in `{dc}` — irreversible",
        "create_nat_rule": f"Create {params.get('rule_type', 'SNAT')} rule on NAT gateway `{params.get('nat_id', '?')}` in `{dc}`",
        "delete_nat_rule": f"**DELETE** NAT rule `{params.get('rule_id', '?')}` — irreversible",
        # Kubernetes
        "create_k8s_cluster": f"Create Kubernetes cluster **{params.get('name', '?')}** (v{params.get('k8s_version', 'latest')})",
        "delete_k8s_cluster": f"**DELETE** Kubernetes cluster `{params.get('cluster_id', '?')}` — irreversible",
        "create_k8s_nodepool": f"Create node pool **{params.get('name', '?')}** ({params.get('node_count', 1)} × {params.get('cores', 2)}c/{params.get('ram_mb', 2048)}MB) in cluster `{params.get('cluster_id', '?')}`",
        "delete_k8s_nodepool": f"**DELETE** node pool `{params.get('nodepool_id', '?')}` from cluster `{params.get('cluster_id', '?')}` — irreversible",
    }
    return descs.get(action, f"IONOS action: **{action}**")


class IONOSCloudSkill(BaseSkill):
    name = "ionos_cloud"
    description = (
        "Manage IONOS cloud servers: provision new VMs, list servers, start/stop/reboot, "
        "check status. Use when Anthony says 'provision a server', 'create VM on IONOS', "
        "'list IONOS servers', 'spin up a new server', 'restart IONOS server', or "
        "'delete server [name]'. Requires CRITICAL approval. NOT for: DNS management (use ionos_dns)."
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
                is_error=True,
                needs_config=True,
            )

        action = params.get("action", "list_datacenters")

        # ── Read-only: execute immediately ────────────────────────────────────
        if action in _READ_ACTIONS:
            try:
                import httpx
                data = await client.execute_action(action, params)
                return SkillResult(
                    context_data=json.dumps(data, indent=2) if not isinstance(data, str) else data,
                    skill_name=self.name,
                )
            except httpx.HTTPStatusError as exc:
                logger.warning("IONOSCloudSkill: HTTP error for action %s: %s", action, exc)
                return SkillResult(
                    context_data=f"[IONOS HTTP error {exc.response.status_code}: {exc}]",
                    skill_name=self.name,
                )
            except ValueError as exc:
                logger.warning("IONOSCloudSkill: invalid action or params for %s: %s", action, exc)
                return SkillResult(context_data=f"[IONOS error: {exc}]", skill_name=self.name)

        # ── Write / destructive: route through confirmation ───────────────────
        if action in _BREAKING_ACTIONS:
            # Use instance attribute to avoid polluting class-level state
            self.approval_category = ApprovalCategory.BREAKING
        else:
            self.approval_category = ApprovalCategory.CRITICAL

        description = _describe_action(action, params)
        pending = {
            "intent": "ionos_cloud",
            "action": action,
            "params": params,
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
    description = (
        "Manage IONOS DNS records: add, update, or delete A/CNAME/TXT/MX records for domains. "
        "Use when Anthony says 'add DNS record', 'point [subdomain] to [IP]', 'create CNAME for', "
        "'update DNS for [domain]', or 'delete DNS record'. NOT for: cloud server management "
        "(use ionos_cloud)."
    )
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
                is_error=True,
                needs_config=True,
            )

        action = params.get("action", "list_zones")
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
            "intent": "ionos_dns",
            "action": action,
            "params": params,
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
            "create_zone": f"Create DNS zone `{params.get('zone_name', '?')}`",
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
