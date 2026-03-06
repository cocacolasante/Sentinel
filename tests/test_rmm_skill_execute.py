"""
Execute-path tests for RMMReadSkill and RMMManageSkill.

All DB and MeshCentral calls are mocked so no live services are needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.skills.base import ApprovalCategory, SkillResult
from app.skills.rmm_skill import (
    _build_label,
    _fmt_ts,
    RMMReadSkill,
    RMMManageSkill,
)


# ── RMMReadSkill — list action ─────────────────────────────────────────────────


async def test_list_from_db_rows():
    """If DB has rows, skip live API call."""
    rows = [
        {"node_id": "n1", "name": "web01", "hostname": "web01.local",
         "ip_address": "10.0.0.1", "os_name": "Ubuntu 22", "is_online": True,
         "group_name": "prod", "project": "sentinel", "last_seen": None,
         "agent_version": "1.0"},
    ]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await RMMReadSkill().execute({"action": "list"}, "show devices")
    assert "web01" in r.context_data
    assert "🟢" in r.context_data


async def test_list_from_db_empty_falls_back_to_api():
    """Empty DB → fall back to live API."""
    mc = MagicMock()
    mc.list_devices = AsyncMock(return_value=[
        {"_id": "n2", "name": "api01", "host": "api01.local", "conn": 0, "ostype": "linux"}
    ])
    with patch("app.db.postgres.execute", return_value=[]), \
         patch("app.integrations.meshcentral.MeshCentralClient", return_value=mc):
        r = await RMMReadSkill().execute({"action": "list"}, "list servers")
    assert "api01" in r.context_data


async def test_list_from_api_empty():
    """Both DB and API empty → informative message."""
    mc = MagicMock()
    mc.list_devices = AsyncMock(return_value=[])
    with patch("app.db.postgres.execute", return_value=[]), \
         patch("app.integrations.meshcentral.MeshCentralClient", return_value=mc):
        r = await RMMReadSkill().execute({"action": "list"}, "list")
    assert "no" in r.context_data.lower() or "found" in r.context_data.lower()


async def test_list_db_exception_falls_back():
    """DB exception triggers live API fallback."""
    mc = MagicMock()
    mc.list_devices = AsyncMock(return_value=[
        {"_id": "n3", "name": "bk01", "host": "", "conn": 1, "ostype": "linux"}
    ])
    with patch("app.db.postgres.execute", side_effect=Exception("DB down")), \
         patch("app.integrations.meshcentral.MeshCentralClient", return_value=mc):
        r = await RMMReadSkill().execute({"action": "list"}, "")
    assert isinstance(r.context_data, str)


async def test_list_with_group_filter():
    rows = [
        {"node_id": "n4", "name": "db01", "is_online": False, "group_name": "db",
         "project": "", "last_seen": "2026-01-01T12:00:00", "ip_address": None,
         "os_name": "debian"},
    ]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await RMMReadSkill().execute({"action": "list", "group": "db"}, "")
    assert "db01" in r.context_data
    assert "🔴" in r.context_data


# ── RMMReadSkill — get action ─────────────────────────────────────────────────


async def test_get_device_missing_id():
    r = await RMMReadSkill().execute({"action": "get"}, "")
    assert "node_id" in r.context_data.lower() or "provide" in r.context_data.lower()


async def test_get_device_from_db():
    row = {
        "node_id": "n1", "name": "web01", "hostname": "web01.local",
        "ip_address": "10.0.0.1", "os_name": "Ubuntu 22", "agent_version": "1.2",
        "group_name": "prod", "project": "sentinel", "is_online": True,
        "last_seen": "2026-01-01T10:00:00", "cpu_usage": 45.2,
        "memory_usage": 60.0, "disk_usage": 30.0,
    }
    with patch("app.db.postgres.execute_one", return_value=row):
        r = await RMMReadSkill().execute({"action": "get", "node_id": "n1"}, "")
    assert "web01" in r.context_data
    assert "45.2" in r.context_data


async def test_get_device_not_found():
    mc = MagicMock()
    mc.get_device = AsyncMock(return_value=None)
    with patch("app.db.postgres.execute_one", return_value=None), \
         patch("app.integrations.meshcentral.MeshCentralClient", return_value=mc):
        r = await RMMReadSkill().execute({"action": "get", "node_id": "missing"}, "")
    assert "not found" in r.context_data.lower()


async def test_get_device_db_exception():
    row = {"node_id": "n5", "name": "ns01", "hostname": "", "ip_address": None,
           "os_name": "linux", "agent_version": None, "group_name": None,
           "project": None, "is_online": False, "last_seen": None,
           "cpu_usage": None}
    mc = MagicMock()
    mc.get_device = AsyncMock(return_value=row)
    with patch("app.db.postgres.execute_one", side_effect=Exception("db error")), \
         patch("app.integrations.meshcentral.MeshCentralClient", return_value=mc):
        r = await RMMReadSkill().execute({"action": "get", "name": "ns01"}, "")
    assert isinstance(r.context_data, str)


# ── RMMReadSkill — status action ──────────────────────────────────────────────


async def test_status_summary():
    with patch("app.db.postgres.execute_one", side_effect=[{"n": 10}, {"n": 8}]), \
         patch("app.db.postgres.execute", return_value=[
             {"event_type": "agent_online", "severity": "info", "cnt": 3}
         ]):
        r = await RMMReadSkill().execute({"action": "status"}, "status")
    assert "10" in r.context_data
    assert "8" in r.context_data


async def test_status_db_exception():
    with patch("app.db.postgres.execute_one", side_effect=Exception("no db")):
        r = await RMMReadSkill().execute({"action": "status"}, "")
    assert "could not" in r.context_data.lower() or "error" in r.context_data.lower()


async def test_status_no_events():
    with patch("app.db.postgres.execute_one", side_effect=[{"n": 5}, {"n": 5}]), \
         patch("app.db.postgres.execute", return_value=[]):
        r = await RMMReadSkill().execute({"action": "status"}, "")
    assert "No events" in r.context_data


# ── RMMReadSkill — events action ──────────────────────────────────────────────


async def test_recent_events_with_results():
    rows = [
        {"event_type": "cpu_high", "severity": "high", "hostname": "web01",
         "node_id": "n1", "group_name": "prod", "created_at": "2026-01-01T12:00:00"}
    ]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await RMMReadSkill().execute({"action": "events"}, "show events")
    assert "cpu_high" in r.context_data


async def test_recent_events_empty():
    with patch("app.db.postgres.execute", return_value=[]):
        r = await RMMReadSkill().execute({"action": "events"}, "")
    assert "no" in r.context_data.lower()


async def test_recent_events_exception():
    with patch("app.db.postgres.execute", side_effect=Exception("conn error")):
        r = await RMMReadSkill().execute({"action": "events", "severity": "critical"}, "")
    assert "could not" in r.context_data.lower()


# ── RMMReadSkill — incidents action ───────────────────────────────────────────


async def test_incidents_with_data():
    rows = [
        {"event_type": "offline", "severity": "high", "hostname": "db01",
         "node_id": "n2", "group_name": "db", "project": "core",
         "details": {}, "created_at": "2026-01-01T11:00:00"}
    ]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await RMMReadSkill().execute({"action": "incidents"}, "show incidents")
    assert "offline" in r.context_data


async def test_incidents_none():
    with patch("app.db.postgres.execute", return_value=[]):
        r = await RMMReadSkill().execute({"action": "incidents", "hours": "48"}, "")
    assert "healthy" in r.context_data.lower() or "no incidents" in r.context_data.lower()


async def test_incidents_exception():
    with patch("app.db.postgres.execute", side_effect=Exception("db fail")):
        r = await RMMReadSkill().execute({"action": "incidents"}, "")
    assert "could not" in r.context_data.lower()


# ── RMMReadSkill — inventory action ───────────────────────────────────────────


async def test_inventory_report():
    rows = [
        {"group_name": "prod", "project": "sentinel", "total": 5, "online": 5},
        {"group_name": "staging", "project": "", "total": 2, "online": 1},
    ]
    with patch("app.db.postgres.execute", return_value=rows):
        r = await RMMReadSkill().execute({"action": "inventory"}, "show inventory")
    assert "prod" in r.context_data
    assert "staging" in r.context_data


async def test_inventory_empty():
    with patch("app.db.postgres.execute", return_value=[]):
        r = await RMMReadSkill().execute({"action": "inventory"}, "")
    assert "no devices" in r.context_data.lower() or "sync" in r.context_data.lower()


async def test_inventory_exception():
    with patch("app.db.postgres.execute", side_effect=Exception("db err")):
        r = await RMMReadSkill().execute({"action": "inventory"}, "")
    assert "could not" in r.context_data.lower()


# ── RMMReadSkill — meshes action ──────────────────────────────────────────────


async def test_list_meshes_with_data():
    mc = MagicMock()
    mc.get_meshes = AsyncMock(return_value=[
        {"_id": "mesh01", "name": "Production"},
        {"id": "mesh02", "name": "Staging"},
    ])
    with patch("app.integrations.meshcentral.MeshCentralClient", return_value=mc):
        r = await RMMReadSkill().execute({"action": "meshes"}, "list meshes")
    assert "Production" in r.context_data


async def test_list_meshes_empty():
    mc = MagicMock()
    mc.get_meshes = AsyncMock(return_value=[])
    with patch("app.integrations.meshcentral.MeshCentralClient", return_value=mc):
        r = await RMMReadSkill().execute({"action": "meshes"}, "")
    assert "no meshes" in r.context_data.lower() or "unreachable" in r.context_data.lower()


# ── RMMReadSkill — unknown action ──────────────────────────────────────────────


async def test_unknown_action():
    r = await RMMReadSkill().execute({"action": "teleport"}, "")
    assert "unknown" in r.context_data.lower()


# ── RMMManageSkill — execute paths ────────────────────────────────────────────


async def test_manage_missing_action():
    r = await RMMManageSkill().execute({}, "do something")
    assert "action" in r.context_data.lower()


async def test_manage_missing_node_id():
    r = await RMMManageSkill().execute({"action": "run_command", "command": "ls"}, "")
    assert "node_id" in r.context_data.lower() or "provide" in r.context_data.lower()


async def test_manage_run_command_builds_pending():
    r = await RMMManageSkill().execute(
        {"action": "run_command", "node_id": "n1", "command": "df -h"},
        "run df on web01",
    )
    assert r.pending_action is not None
    assert r.pending_action["action"] == "rmm_run_command"
    assert "n1" in r.context_data


async def test_manage_restart_service_builds_pending():
    r = await RMMManageSkill().execute(
        {"action": "restart_service", "node_id": "n2", "service": "nginx"},
        "restart nginx",
    )
    assert r.pending_action is not None
    assert "nginx" in r.context_data


async def test_manage_restart_container_builds_pending():
    r = await RMMManageSkill().execute(
        {"action": "restart_container", "node_id": "n3", "container": "brain"},
        "restart brain container",
    )
    assert r.pending_action is not None
    assert "brain" in r.context_data


async def test_manage_reboot_builds_pending():
    r = await RMMManageSkill().execute(
        {"action": "reboot", "node_id": "n4"},
        "reboot server",
    )
    assert r.pending_action is not None
    assert "reboot" in r.context_data.lower()


async def test_manage_upgrade_agent_lowers_approval():
    skill = RMMManageSkill()
    r = await skill.execute(
        {"action": "upgrade_agent", "node_id": "n5"},
        "upgrade agent",
    )
    assert r.pending_action is not None
    assert skill.approval_category == ApprovalCategory.STANDARD


async def test_manage_install_agent_no_node_id():
    """install_agent doesn't need a node_id — builds pending immediately."""
    r = await RMMManageSkill().execute(
        {"action": "install_agent", "mesh_id": "mesh01"},
        "install agent on new server",
    )
    assert r.pending_action is not None


# ── _build_label coverage ─────────────────────────────────────────────────────


def test_build_label_run_command():
    lbl = _build_label("run_command", {"node_id": "web01", "command": "uptime"})
    assert "web01" in lbl and "uptime" in lbl


def test_build_label_restart_service():
    lbl = _build_label("restart_service", {"node_id": "web01", "service": "nginx"})
    assert "nginx" in lbl


def test_build_label_restart_container():
    lbl = _build_label("restart_container", {"node_id": "web01", "container": "app"})
    assert "app" in lbl


def test_build_label_reboot():
    lbl = _build_label("reboot", {"node_id": "srv01"})
    assert "srv01" in lbl and "reboot" in lbl.lower()


def test_build_label_upgrade_agent():
    lbl = _build_label("upgrade_agent", {"node_id": "srv01"})
    assert "upgrade" in lbl.lower() or "agent" in lbl.lower()


def test_build_label_install_agent():
    lbl = _build_label("install_agent", {"mesh_id": "mesh01"})
    assert "install" in lbl.lower() or "agent" in lbl.lower()


def test_build_label_unknown():
    lbl = _build_label("teleport", {"node_id": "srv01"})
    assert isinstance(lbl, str)


# ── _fmt_ts coverage ──────────────────────────────────────────────────────────


def test_fmt_ts_none():
    assert _fmt_ts(None) == "N/A"


def test_fmt_ts_string():
    assert _fmt_ts("2026-01-01T12:34:56") == "2026-01-01 12:34"


def test_fmt_ts_datetime():
    from datetime import datetime, timezone
    dt = datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc)
    result = _fmt_ts(dt)
    assert "2026-03-01" in result


def test_fmt_ts_number():
    result = _fmt_ts(1000000.0)
    assert isinstance(result, str)
