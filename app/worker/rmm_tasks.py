"""
RMM Celery Tasks — background polling, incident detection, WebSocket listener

Schedule (defined in celery_app.py beat_schedule):
  rmm-device-poll      : every 60 seconds — online/offline state check
  rmm-full-sync        : every 5 minutes  — full device inventory sync to DB
  rmm-incident-check   : every 2 minutes  — threshold breach detection + Slack alerts
  rmm-websocket-listen : on-demand        — long-running WebSocket event listener
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# Thresholds for automated incident detection
_CPU_WARN = 90.0
_MEM_WARN = 85.0
_DISK_WARN = 90.0
_OFFLINE_ALERT_MINUTES = 5


# ── Poll device status (every 60 seconds) ─────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.rmm_tasks.rmm_poll_device_status",
    queue="celery",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=55,
    time_limit=60,
)
def rmm_poll_device_status(self) -> dict:
    """
    Fetch all device online/offline states from MeshCentral, compare to DB,
    detect transitions and alert Slack on unexpected offline events.
    """
    try:
        return asyncio.run(_poll_device_status())
    except Exception as exc:
        logger.error("rmm_poll_device_status failed: %s", exc)
        raise self.retry(exc=exc)


async def _poll_device_status() -> dict:
    from app.integrations.meshcentral import MeshCentralClient
    from app.db import postgres
    from app.config import get_settings

    client = MeshCentralClient()
    if not client.is_configured():
        return {"skipped": "MeshCentral not configured"}

    devices = await client.list_devices()
    if not devices:
        logger.debug("rmm_poll: no devices returned")
        return {"devices": 0}

    s = get_settings()
    alerts: list[str] = []
    now = datetime.now(timezone.utc)

    for dev in devices:
        node_id = dev.get("_id") or dev.get("id", "")
        if not node_id:
            continue
        name = dev.get("name", node_id)
        is_online = dev.get("conn", 0) == 1

        # Read previous state from DB
        try:
            prev = postgres.execute_one(
                "SELECT is_online, group_name, project FROM rmm_devices WHERE node_id = %s",
                (node_id,),
            )
        except Exception:
            prev = None

        was_online = prev.get("is_online") if prev else None
        group = (prev or {}).get("group_name", "")
        project = (prev or {}).get("project", "")

        # Detect offline transition
        if was_online is True and not is_online:
            severity = "critical" if group == "production" else "medium"
            msg = (
                f"🔴 *RMM ALERT* — `{name}` went **offline**"
                + (f" [{group}]" if group else "")
                + (f" / {project}" if project else "")
            )
            alerts.append(msg)
            _store_event(node_id, name, "agent_disconnect", severity, group, project, {})

        # Detect online recovery
        if was_online is False and is_online:
            msg = (
                f"🟢 *RMM* — `{name}` is back **online**"
                + (f" [{group}]" if group else "")
            )
            alerts.append(msg)
            _store_event(node_id, name, "agent_connect", "info", group, project, {})

        # Upsert online status + last_seen
        try:
            postgres.execute(
                """
                INSERT INTO rmm_devices (node_id, name, is_online, last_seen, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (node_id) DO UPDATE
                  SET is_online  = EXCLUDED.is_online,
                      last_seen  = CASE WHEN EXCLUDED.is_online THEN EXCLUDED.last_seen
                                        ELSE rmm_devices.last_seen END,
                      updated_at = EXCLUDED.updated_at
                """,
                (node_id, name, is_online, now if is_online else None, now),
            )
        except Exception as exc:
            logger.warning("rmm_poll upsert failed for %s: %s", node_id, exc)

    if alerts:
        _post_rmm_alerts(alerts, s)

    return {"polled": len(devices), "alerts": len(alerts)}


# ── Full inventory sync (every 5 minutes) ─────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.rmm_tasks.rmm_full_inventory_sync",
    queue="celery",
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=290,
    time_limit=300,
)
def rmm_full_inventory_sync(self) -> dict:
    """Sync full device metadata (hostname, OS, IP, agent version, groups) to DB."""
    try:
        return asyncio.run(_full_inventory_sync())
    except Exception as exc:
        logger.error("rmm_full_inventory_sync failed: %s", exc)
        raise self.retry(exc=exc)


async def _full_inventory_sync() -> dict:
    from app.integrations.meshcentral import MeshCentralClient
    from app.db import postgres

    client = MeshCentralClient()
    if not client.is_configured():
        return {"skipped": "MeshCentral not configured"}

    devices = await client.list_devices()
    if not devices:
        return {"synced": 0}

    now = datetime.now(timezone.utc)
    synced = 0

    for dev in devices:
        node_id = dev.get("_id") or dev.get("id", "")
        if not node_id:
            continue

        name = dev.get("name", "")
        hostname = dev.get("host", "") or dev.get("rname", "")
        ip_addr = _extract_ip(dev)
        os_name = _extract_os(dev)
        agent_ver = str(dev.get("agentver", "") or "")
        is_online = dev.get("conn", 0) == 1
        mesh_id = dev.get("meshid", "")

        # Infer group + project from mesh name or tags
        group_name = _infer_group(dev)
        project = _infer_project(dev)

        try:
            postgres.execute(
                """
                INSERT INTO rmm_devices
                    (node_id, name, hostname, ip_address, os_name, agent_version,
                     mesh_id, group_name, project, is_online, last_seen,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (node_id) DO UPDATE
                  SET name          = EXCLUDED.name,
                      hostname      = EXCLUDED.hostname,
                      ip_address    = COALESCE(EXCLUDED.ip_address, rmm_devices.ip_address),
                      os_name       = COALESCE(EXCLUDED.os_name, rmm_devices.os_name),
                      agent_version = EXCLUDED.agent_version,
                      mesh_id       = EXCLUDED.mesh_id,
                      group_name    = COALESCE(EXCLUDED.group_name, rmm_devices.group_name),
                      project       = COALESCE(EXCLUDED.project, rmm_devices.project),
                      is_online     = EXCLUDED.is_online,
                      last_seen     = CASE WHEN EXCLUDED.is_online
                                          THEN EXCLUDED.last_seen
                                          ELSE rmm_devices.last_seen END,
                      updated_at    = EXCLUDED.updated_at
                """,
                (
                    node_id, name, hostname, ip_addr, os_name, agent_ver,
                    mesh_id, group_name, project, is_online,
                    now if is_online else None, now, now,
                ),
            )
            synced += 1
        except Exception as exc:
            logger.warning("rmm_sync upsert failed for %s: %s", node_id, exc)

    logger.info("RMM full sync complete: %d devices", synced)
    return {"synced": synced}


# ── Incident detection (every 2 minutes) ──────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.rmm_tasks.rmm_incident_detection",
    queue="celery",
    max_retries=2,
    default_retry_delay=30,
    soft_time_limit=110,
    time_limit=120,
)
def rmm_incident_detection(self) -> dict:
    """
    Check all online devices for resource threshold breaches.
    Correlates with Prometheus where available. Posts Slack alerts.
    """
    try:
        return asyncio.run(_incident_detection())
    except Exception as exc:
        logger.error("rmm_incident_detection failed: %s", exc)
        raise self.retry(exc=exc)


async def _incident_detection() -> dict:
    from app.db import postgres
    from app.config import get_settings

    s = get_settings()
    alerts: list[str] = []

    # Check for devices offline longer than threshold
    try:
        offline_rows = postgres.execute(
            f"""
            SELECT node_id, name, group_name, project, last_seen
            FROM rmm_devices
            WHERE is_online = FALSE
              AND last_seen IS NOT NULL
              AND last_seen < NOW() - INTERVAL '{_OFFLINE_ALERT_MINUTES} minutes'
            """
        )
        for r in offline_rows:
            last = _fmt_ts(r.get("last_seen"))
            alerts.append(
                f"🔴 *RMM* — `{r['name']}` offline since {last}"
                + (f" [{r.get('group_name', '')}]" if r.get("group_name") else "")
            )
    except Exception as exc:
        logger.warning("rmm_incident_detection offline check failed: %s", exc)

    # Check Prometheus for high-resource devices
    try:
        from app.integrations.prometheus_client import PrometheusClient, METRIC_QUERIES
        prom = PrometheusClient()
        if prom.is_available():
            cpu_result = await prom.query(METRIC_QUERIES["cpu"])
            if cpu_result:
                for series in cpu_result:
                    val = float(series["value"][1])
                    if val > _CPU_WARN:
                        alerts.append(
                            f"🟠 *RMM* — High CPU alert: {val:.1f}% (threshold {_CPU_WARN}%)"
                        )

            mem_result = await prom.query(METRIC_QUERIES["memory"])
            if mem_result:
                for series in mem_result:
                    val = float(series["value"][1])
                    if val > _MEM_WARN:
                        alerts.append(
                            f"🟠 *RMM* — High memory alert: {val:.1f}% (threshold {_MEM_WARN}%)"
                        )
    except Exception as exc:
        logger.debug("Prometheus correlation skipped: %s", exc)

    if alerts:
        _post_rmm_alerts(alerts, s)

    return {"incidents_detected": len(alerts)}


# ── WebSocket listener (on-demand, long-running) ──────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.rmm_tasks.rmm_websocket_listener",
    queue="celery",
    max_retries=0,
    soft_time_limit=82_800,   # 23h — beat will restart it daily
    time_limit=86_400,        # 24h hard limit
)
def rmm_websocket_listener(self) -> dict:
    """
    Long-running WebSocket event listener.
    Connects to MeshCentral, receives real-time events, stores to DB,
    and posts Slack alerts for critical/medium events.
    """
    try:
        return asyncio.run(_run_websocket_listener())
    except Exception as exc:
        logger.error("rmm_websocket_listener crashed: %s", exc)
        return {"error": str(exc)}


async def _run_websocket_listener() -> dict:
    from app.integrations.meshcentral import MeshCentralClient
    from app.config import get_settings

    client = MeshCentralClient()
    if not client.is_configured():
        return {"skipped": "MeshCentral not configured"}

    s = get_settings()
    event_count = 0
    stop = asyncio.Event()

    def handle_event(event: dict) -> None:
        nonlocal event_count
        event_count += 1
        _store_event(
            node_id=event.get("node_id", ""),
            hostname=event.get("host", ""),
            event_type=event.get("event_type", "unknown"),
            severity=event.get("severity", "info"),
            group_name="",
            project="",
            details=event.get("details", {}),
        )
        # Post Slack alert for high-severity events
        if event.get("severity") in ("critical", "high", "medium"):
            badge = "🔴" if event.get("severity") in ("critical", "high") else "🟠"
            msg = (
                f"{badge} *RMM Event* — `{event.get('event_type')}` "
                f"on `{event.get('host', '?')}`"
            )
            _post_rmm_alerts([msg], s)

    logger.info("Starting MeshCentral WebSocket listener")
    await client.subscribe_events(handle_event, stop_event=stop)
    return {"events_processed": event_count}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _store_event(
    node_id: str,
    hostname: str,
    event_type: str,
    severity: str,
    group_name: str,
    project: str,
    details: dict,
) -> None:
    """Persist an RMM event to the database."""
    import json as _json
    from app.db import postgres

    try:
        postgres.execute(
            """
            INSERT INTO rmm_events
                (node_id, event_type, severity, hostname, project, group_name, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                node_id, event_type, severity, hostname, project, group_name,
                _json.dumps(details),
            ),
        )
    except Exception as exc:
        logger.warning("rmm_store_event failed: %s", exc)


def _post_rmm_alerts(messages: list[str], settings) -> None:
    """Post alert messages to the appropriate Slack RMM channels."""
    from app.integrations.slack_notifier import post_alert_sync

    # Route by severity / group context — for now, post all to prod channel
    # (production server events) or dev channel (dev/staging servers)
    for msg in messages:
        is_prod = (
            "[production]" in msg.lower()
            or "prod" in msg.lower()
            or "critical" in msg.lower()
        )
        channel = (
            settings.slack_rmm_prod_channel
            if is_prod
            else settings.slack_rmm_dev_channel
        )
        try:
            post_alert_sync(msg, channel)
        except Exception as exc:
            logger.warning("rmm slack alert failed: %s", exc)


def _extract_ip(dev: dict) -> str:
    """Extract the primary IP address from a MeshCentral device dict."""
    # MeshCentral stores IPs in nested arrays
    nics = dev.get("netif", [])
    if isinstance(nics, list):
        for nic in nics:
            if isinstance(nic, dict):
                addrs = nic.get("addrs", [])
                for addr in (addrs if isinstance(addrs, list) else []):
                    if isinstance(addr, str) and not addr.startswith("127.") and ":" not in addr:
                        return addr
    return dev.get("host", "") or ""


def _extract_os(dev: dict) -> str:
    os_type = dev.get("ostype", 0)
    os_desc = dev.get("osdesc", "")
    if os_desc:
        return os_desc
    if os_type == 0:
        return "unknown"
    if os_type == 1:
        return "Windows"
    if os_type == 2:
        return "Linux"
    if os_type == 3:
        return "macOS"
    return f"OS type {os_type}"


def _infer_group(dev: dict) -> str:
    """Infer production/staging/dev group from device name or tags."""
    name = (dev.get("name") or "").lower()
    if "prod" in name or "production" in name:
        return "production"
    if "staging" in name or "stg" in name:
        return "staging"
    if "dev" in name or "develop" in name:
        return "dev"
    return ""


def _infer_project(dev: dict) -> str:
    """Infer project name from device name conventions."""
    name = (dev.get("name") or "").lower()
    known = ["sentinel", "language-tutor", "n8n", "langtutor"]
    for k in known:
        if k in name:
            return k
    return ""


def _fmt_ts(ts) -> str:
    if not ts:
        return "N/A"
    if isinstance(ts, str):
        return ts[:16].replace("T", " ")
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)[:16]
