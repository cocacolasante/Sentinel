"""
Unit tests for the RMM skill, MeshCentral integration, and worker helpers.

Covers pure functions and class metadata only — no network or DB calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── meshcentral._normalize_event ─────────────────────────────────────────────


from app.integrations.meshcentral import _normalize_event, MeshCentralClient


def test_normalize_event_agent_connect():
    msg = {
        "action": "nodeconnect",
        "node": {"_id": "node//abc", "name": "web-prod-01"},
        "time": "2026-03-06T00:00:00Z",
    }
    ev = _normalize_event(msg)
    assert ev is not None
    assert ev["event_type"] == "agent_connect"
    assert ev["host"] == "web-prod-01"
    assert ev["node_id"] == "node//abc"
    assert ev["severity"] == "info"


def test_normalize_event_agent_disconnect():
    msg = {
        "action": "nodedisconnect",
        "node": {"_id": "node//xyz", "name": "db-prod-01"},
    }
    ev = _normalize_event(msg)
    assert ev is not None
    assert ev["event_type"] == "agent_disconnect"
    assert ev["severity"] == "medium"


def test_normalize_event_ping_returns_none():
    assert _normalize_event({"action": "ping"}) is None


def test_normalize_event_pong_returns_none():
    assert _normalize_event({"action": "pong"}) is None


def test_normalize_event_login_returns_none():
    assert _normalize_event({"action": "login"}) is None


def test_normalize_event_serverinfo_returns_none():
    assert _normalize_event({"action": "serverinfo"}) is None


def test_normalize_event_empty_action_returns_none():
    assert _normalize_event({"action": ""}) is None
    assert _normalize_event({}) is None


def test_normalize_event_unknown_action_uses_action_as_type():
    msg = {"action": "custom_event", "nodeid": "n1"}
    ev = _normalize_event(msg)
    assert ev is not None
    assert ev["event_type"] == "custom_event"


def test_normalize_event_console_output():
    msg = {
        "action": "console",
        "nodeid": "node//test",
        "output": "Hello world",
    }
    ev = _normalize_event(msg)
    assert ev["event_type"] == "console_output"
    assert ev["details"]["output"] == "Hello world"


def test_normalize_event_command_result():
    msg = {
        "action": "runcommands",
        "nodeid": "node//srv1",
        "result": "ok",
    }
    ev = _normalize_event(msg)
    assert ev["event_type"] == "command_result"
    assert ev["details"]["result"] == "ok"


def test_normalize_event_has_timestamp():
    msg = {"action": "nodeconnect", "node": {"_id": "x"}, "time": "2026-01-01T12:00:00Z"}
    ev = _normalize_event(msg)
    assert ev["timestamp"] == "2026-01-01T12:00:00Z"


def test_normalize_event_fallback_timestamp():
    """When no 'time' field, timestamp should be a non-empty string."""
    msg = {"action": "nodeconnect", "node": {"_id": "x"}}
    ev = _normalize_event(msg)
    assert isinstance(ev["timestamp"], str)
    assert len(ev["timestamp"]) > 0


def test_normalize_event_node_id_from_nodeid_field():
    msg = {"action": "console", "nodeid": "node//fallback"}
    ev = _normalize_event(msg)
    assert ev["node_id"] == "node//fallback"


# ── MeshCentralClient metadata ────────────────────────────────────────────────


def test_meshcentral_not_configured_when_no_url():
    with patch("app.integrations.meshcentral._settings") as mock_s:
        mock_s.return_value = MagicMock(
            meshcentral_url="", meshcentral_user="admin", meshcentral_password="pw"
        )
        client = MeshCentralClient()
        assert client.is_configured() is False


def test_meshcentral_not_configured_when_no_user():
    with patch("app.integrations.meshcentral._settings") as mock_s:
        mock_s.return_value = MagicMock(
            meshcentral_url="https://mesh.example.com",
            meshcentral_user="",
            meshcentral_password="pw",
            meshcentral_domain="",
        )
        client = MeshCentralClient()
        assert client.is_configured() is False


def test_meshcentral_not_configured_when_no_password():
    with patch("app.integrations.meshcentral._settings") as mock_s:
        mock_s.return_value = MagicMock(
            meshcentral_url="https://mesh.example.com",
            meshcentral_user="admin",
            meshcentral_password="",
            meshcentral_domain="",
        )
        client = MeshCentralClient()
        assert client.is_configured() is False


def test_meshcentral_is_configured_when_all_set():
    with patch("app.integrations.meshcentral._settings") as mock_s:
        mock_s.return_value = MagicMock(
            meshcentral_url="https://mesh.example.com",
            meshcentral_user="admin",
            meshcentral_password="secret",
            meshcentral_domain="",
        )
        client = MeshCentralClient()
        assert client.is_configured() is True


def test_get_agent_install_command_linux():
    with patch("app.integrations.meshcentral._settings") as mock_s:
        mock_s.return_value = MagicMock(
            meshcentral_url="https://mesh.example.com",
            meshcentral_user="admin",
            meshcentral_password="pw",
            meshcentral_domain="",
        )
        client = MeshCentralClient()
        cmd = client.get_agent_install_command("mesh123", "linux")
        assert "mesh.example.com" in cmd
        assert "mesh123" in cmd
        assert "curl" in cmd or "wget" in cmd


def test_get_agent_install_command_windows():
    with patch("app.integrations.meshcentral._settings") as mock_s:
        mock_s.return_value = MagicMock(
            meshcentral_url="https://mesh.example.com",
            meshcentral_user="admin",
            meshcentral_password="pw",
            meshcentral_domain="",
        )
        client = MeshCentralClient()
        cmd = client.get_agent_install_command("mesh456", "windows")
        assert "powershell" in cmd.lower()
        assert "mesh456" in cmd


def test_get_agent_install_script_url():
    with patch("app.integrations.meshcentral._settings") as mock_s:
        mock_s.return_value = MagicMock(
            meshcentral_url="https://mesh.example.com",
            meshcentral_user="admin",
            meshcentral_password="pw",
            meshcentral_domain="",
        )
        client = MeshCentralClient()
        url = client.get_agent_install_script_url("mymesh")
        assert url.startswith("https://mesh.example.com")
        assert "mymesh" in url
        assert "meshagents" in url


# ── rmm_skill helpers ─────────────────────────────────────────────────────────


from app.skills.rmm_skill import _fmt_ts, _build_label, RMMReadSkill, RMMManageSkill


def test_fmt_ts_none_returns_na():
    assert _fmt_ts(None) == "N/A"


def test_fmt_ts_string_truncated():
    assert _fmt_ts("2026-03-06T12:34:56Z") == "2026-03-06 12:34"


def test_fmt_ts_datetime_object():
    dt = datetime(2026, 3, 6, 8, 30, tzinfo=timezone.utc)
    result = _fmt_ts(dt)
    assert "2026-03-06" in result
    assert "08:30" in result


def test_fmt_ts_other_type_returns_string():
    result = _fmt_ts(12345678)
    assert isinstance(result, str)


def test_build_label_run_command():
    label = _build_label("run_command", {"node_id": "web-01", "command": "df -h"})
    assert "web-01" in label
    assert "df -h" in label


def test_build_label_restart_service():
    label = _build_label("restart_service", {"node_id": "db-01", "service": "nginx"})
    assert "nginx" in label
    assert "db-01" in label


def test_build_label_restart_container():
    label = _build_label("restart_container", {"node_id": "srv", "container": "api"})
    assert "api" in label
    assert "srv" in label


def test_build_label_reboot():
    label = _build_label("reboot", {"node_id": "staging-01"})
    assert "staging-01" in label
    assert "eboot" in label.lower()


def test_build_label_upgrade_agent():
    label = _build_label("upgrade_agent", {"node_id": "old-server"})
    assert "old-server" in label
    assert "agent" in label.lower()


def test_build_label_install_agent():
    label = _build_label("install_agent", {"host": "192.168.1.10", "mesh_id": "m1"})
    assert "192.168.1.10" in label
    assert "m1" in label


def test_build_label_unknown_action():
    label = _build_label("do_something_weird", {"node_id": "srv"})
    assert "do_something_weird" in label


def test_build_label_uses_name_fallback():
    label = _build_label("reboot", {"name": "my-server"})
    assert "my-server" in label


# ── RMMReadSkill metadata ─────────────────────────────────────────────────────


def test_rmm_read_skill_name():
    assert RMMReadSkill.name == "rmm_read"


def test_rmm_read_skill_has_trigger_intent():
    assert "rmm_read" in RMMReadSkill.trigger_intents


def test_rmm_read_skill_approval_is_none():
    from app.skills.base import ApprovalCategory
    assert RMMReadSkill.approval_category == ApprovalCategory.NONE


def test_rmm_read_skill_not_available_when_unconfigured():
    skill = RMMReadSkill()
    with patch("app.integrations.meshcentral.MeshCentralClient.is_configured", return_value=False):
        assert skill.is_available() is False


def test_rmm_read_skill_available_when_configured():
    skill = RMMReadSkill()
    with patch("app.integrations.meshcentral.MeshCentralClient.is_configured", return_value=True):
        assert skill.is_available() is True


# ── RMMManageSkill metadata ───────────────────────────────────────────────────


def test_rmm_manage_skill_name():
    assert RMMManageSkill.name == "rmm_manage"


def test_rmm_manage_skill_has_trigger_intent():
    assert "rmm_manage" in RMMManageSkill.trigger_intents


def test_rmm_manage_skill_approval_is_critical():
    from app.skills.base import ApprovalCategory
    assert RMMManageSkill.approval_category == ApprovalCategory.CRITICAL


def test_rmm_manage_skill_not_available_when_unconfigured():
    skill = RMMManageSkill()
    with patch("app.integrations.meshcentral.MeshCentralClient.is_configured", return_value=False):
        assert skill.is_available() is False


@pytest.mark.asyncio
async def test_rmm_manage_no_action_returns_help():
    skill = RMMManageSkill()
    result = await skill.execute({"action": ""}, "do something")
    assert "action" in result.context_data.lower()


@pytest.mark.asyncio
async def test_rmm_manage_no_node_id_returns_error():
    skill = RMMManageSkill()
    result = await skill.execute({"action": "run_command"}, "run cmd")
    assert "node_id" in result.context_data.lower() or "name" in result.context_data.lower()


@pytest.mark.asyncio
async def test_rmm_manage_returns_pending_action():
    skill = RMMManageSkill()
    result = await skill.execute(
        {"action": "run_command", "node_id": "web-01", "command": "uptime"},
        "run uptime on web-01",
    )
    assert result.pending_action is not None
    assert result.pending_action["action"] == "rmm_run_command"


@pytest.mark.asyncio
async def test_rmm_manage_install_agent_no_node_required():
    skill = RMMManageSkill()
    result = await skill.execute(
        {"action": "install_agent", "host": "10.0.0.5", "mesh_id": "abc"},
        "install agent on 10.0.0.5",
    )
    assert result.pending_action is not None
    assert result.pending_action["action"] == "rmm_install_agent"


# ── rmm_tasks helpers ─────────────────────────────────────────────────────────


from app.worker.rmm_tasks import (
    _extract_ip,
    _extract_os,
    _infer_group,
    _infer_project,
    _fmt_ts as tasks_fmt_ts,
)


def test_extract_ip_from_netif():
    dev = {
        "netif": [
            {"addrs": ["127.0.0.1", "10.0.1.5"]},
        ]
    }
    assert _extract_ip(dev) == "10.0.1.5"


def test_extract_ip_skips_loopback():
    dev = {"netif": [{"addrs": ["127.0.0.1"]}]}
    # loopback only — falls back to host field
    assert _extract_ip(dev) == ""


def test_extract_ip_skips_ipv6():
    dev = {"netif": [{"addrs": ["::1", "192.168.1.10"]}]}
    assert _extract_ip(dev) == "192.168.1.10"


def test_extract_ip_falls_back_to_host_field():
    dev = {"host": "192.168.5.5"}
    assert _extract_ip(dev) == "192.168.5.5"


def test_extract_ip_empty_device():
    assert _extract_ip({}) == ""


def test_extract_os_from_description():
    dev = {"osdesc": "Ubuntu 22.04 LTS"}
    assert _extract_os(dev) == "Ubuntu 22.04 LTS"


def test_extract_os_windows_type():
    assert _extract_os({"ostype": 1}) == "Windows"


def test_extract_os_linux_type():
    assert _extract_os({"ostype": 2}) == "Linux"


def test_extract_os_macos_type():
    assert _extract_os({"ostype": 3}) == "macOS"


def test_extract_os_unknown_type():
    result = _extract_os({"ostype": 99})
    assert "99" in result


def test_extract_os_zero_type():
    assert _extract_os({"ostype": 0}) == "unknown"


def test_infer_group_production():
    assert _infer_group({"name": "api-prod-01"}) == "production"
    assert _infer_group({"name": "production-db"}) == "production"


def test_infer_group_staging():
    assert _infer_group({"name": "api-staging-01"}) == "staging"
    assert _infer_group({"name": "stg-server"}) == "staging"


def test_infer_group_dev():
    assert _infer_group({"name": "dev-worker"}) == "dev"
    assert _infer_group({"name": "develop-01"}) == "dev"


def test_infer_group_unknown():
    assert _infer_group({"name": "random-server"}) == ""


def test_infer_group_empty_name():
    assert _infer_group({}) == ""


def test_infer_project_sentinel():
    assert _infer_project({"name": "sentinel-brain"}) == "sentinel"


def test_infer_project_n8n():
    assert _infer_project({"name": "n8n-automation"}) == "n8n"


def test_infer_project_language_tutor():
    assert _infer_project({"name": "language-tutor-api"}) == "language-tutor"


def test_infer_project_unknown():
    assert _infer_project({"name": "random-server"}) == ""


def test_tasks_fmt_ts_none():
    assert tasks_fmt_ts(None) == "N/A"


def test_tasks_fmt_ts_string():
    assert tasks_fmt_ts("2026-03-06T10:00:00Z") == "2026-03-06 10:00"


def test_tasks_fmt_ts_datetime():
    dt = datetime(2026, 3, 6, 14, 45, tzinfo=timezone.utc)
    result = tasks_fmt_ts(dt)
    assert "2026-03-06" in result


# ── Config: MeshCentral defaults ──────────────────────────────────────────────


from app.config import Settings

_EMPTY_MC_ENV = {
    "MESHCENTRAL_URL": "",
    "MESHCENTRAL_INTERNAL_URL": "",
    "MESHCENTRAL_USER": "",
    "MESHCENTRAL_PASSWORD": "",
    "MESHCENTRAL_DOMAIN": "",
    "MESHCENTRAL_DEFAULT_MESH_ID": "",
}


def test_meshcentral_url_default_empty():
    with patch.dict("os.environ", _EMPTY_MC_ENV):
        s = Settings(_env_file=None)
        assert s.meshcentral_url == ""


def test_meshcentral_user_default_empty():
    with patch.dict("os.environ", _EMPTY_MC_ENV):
        s = Settings(_env_file=None)
        assert s.meshcentral_user == ""


def test_meshcentral_password_default_empty():
    with patch.dict("os.environ", _EMPTY_MC_ENV):
        s = Settings(_env_file=None)
        assert s.meshcentral_password == ""


def test_meshcentral_domain_default_empty():
    with patch.dict("os.environ", _EMPTY_MC_ENV):
        s = Settings(_env_file=None)
        assert s.meshcentral_domain == ""


def test_meshcentral_default_mesh_id_empty():
    with patch.dict("os.environ", _EMPTY_MC_ENV):
        s = Settings(_env_file=None)
        assert s.meshcentral_default_mesh_id == ""


def test_slack_rmm_prod_channel_default():
    s = Settings()
    assert s.slack_rmm_prod_channel == "rmm-production"


def test_slack_rmm_dev_channel_default():
    s = Settings()
    assert s.slack_rmm_dev_channel == "rmm-dev-staging"


def test_meshcentral_fields_can_be_set():
    s = Settings(
        meshcentral_url="https://mesh.example.com",
        meshcentral_user="admin",
        meshcentral_password="secret",
        meshcentral_domain="myorg",
        meshcentral_default_mesh_id="abc123",
    )
    assert s.meshcentral_url == "https://mesh.example.com"
    assert s.meshcentral_user == "admin"
    assert s.meshcentral_password == "secret"
    assert s.meshcentral_domain == "myorg"
    assert s.meshcentral_default_mesh_id == "abc123"
