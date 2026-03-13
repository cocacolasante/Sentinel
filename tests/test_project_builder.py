"""
Tests for Phase 2 project builder skills.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch


# ── ProjectScaffoldSkill ──────────────────────────────────────────────────────

@patch("app.skills.project_scaffold_skill.subprocess.run")
@patch("app.skills.project_scaffold_skill.postgres.execute")
@patch("anthropic.Anthropic")
def test_project_scaffold_calls_opus_with_extended_thinking(
    mock_anthropic_cls, mock_pg, mock_subprocess
):
    import asyncio
    from pathlib import Path
    from app.skills.project_scaffold_skill import ProjectScaffoldSkill

    scaffold_json = json.dumps({
        "slug": "widget-api",
        "description": "FastAPI CRUD service",
        "tech_stack": "FastAPI",
        "files": [
            {"path": "main.py", "content": "# main\n"},
            {"path": "README.md", "content": "# Widget API\n"},
        ],
    })

    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=scaffold_json)]
    mock_client.messages.create.return_value = mock_msg
    mock_subprocess.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

    project_dir = Path("/root/projects/widget-api")

    with patch("app.skills.project_scaffold_skill.Path.exists", side_effect=lambda: False):
        skill = ProjectScaffoldSkill()
        with patch.object(Path, "mkdir"), patch.object(Path, "write_text"):
            result = asyncio.run(
                skill.execute(
                    {"description": "FastAPI CRUD service", "slug": "widget-api"},
                    "scaffold a widget api",
                )
            )

    # Verify Opus was called with extended thinking
    call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs is not None
    kwargs = call_kwargs[1] if call_kwargs[1] else {}
    args = call_kwargs[0] if call_kwargs[0] else ()
    # Check thinking param present
    create_call = mock_client.messages.create.call_args_list[0]
    _, kw = create_call
    assert "thinking" in kw
    assert kw["thinking"]["type"] == "enabled"
    assert kw["model"] is not None  # model_opus


# ── DependencyAuditSkill ──────────────────────────────────────────────────────

@patch("app.skills.dependency_audit_skill.postgres.execute")
def test_dependency_audit_queries_osv_api(mock_pg, tmp_path):
    import asyncio
    from app.skills.dependency_audit_skill import DependencyAuditSkill

    req_txt = tmp_path / "requirements.txt"
    req_txt.write_text("requests==2.28.0\nflask==2.0.1\n")

    async def fake_query(session, pkg, version, ecosystem):
        return {"vulns": []}

    with patch("app.skills.dependency_audit_skill._query_osv", side_effect=fake_query):
        with patch("app.skills.dependency_audit_skill.aiohttp") as mock_aiohttp:
            mock_session = AsyncMock()
            mock_aiohttp.ClientSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_aiohttp.ClientSession.return_value.__aexit__ = AsyncMock(return_value=False)

            skill = DependencyAuditSkill()
            result = asyncio.run(
                skill.execute({"repo_path": str(tmp_path)}, "audit deps")
            )

    data = json.loads(result.context_data)
    assert "cve_count" in data
    assert "report" in data


@patch("app.skills.dependency_audit_skill.postgres.execute")
def test_dependency_audit_returns_cve_table(mock_pg, tmp_path):
    import asyncio
    from app.skills.dependency_audit_skill import DependencyAuditSkill

    req_txt = tmp_path / "requirements.txt"
    req_txt.write_text("requests==2.28.0\n")

    cve_response = {
        "vulns": [
            {"id": "CVE-2023-1234", "summary": "Remote code execution in requests", "aliases": []},
            {"id": "CVE-2023-5678", "summary": "SSRF vulnerability in requests", "aliases": []},
        ]
    }

    async def fake_query(session, pkg, version, ecosystem):
        return cve_response

    with patch("app.skills.dependency_audit_skill._query_osv", side_effect=fake_query):
        with patch("app.skills.dependency_audit_skill.aiohttp") as mock_aiohttp:
            mock_session = AsyncMock()
            mock_aiohttp.ClientSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_aiohttp.ClientSession.return_value.__aexit__ = AsyncMock(return_value=False)

            skill = DependencyAuditSkill()
            result = asyncio.run(
                skill.execute({"repo_path": str(tmp_path)}, "check for CVEs")
            )

    data = json.loads(result.context_data)
    assert data["cve_count"] == 2
    assert "CVE-2023-1234" in data["report"]
    assert "CVE-2023-5678" in data["report"]


# ── TestGeneratorSkill ────────────────────────────────────────────────────────

@patch("anthropic.Anthropic")
def test_test_generator_writes_pytest_file(mock_anthropic_cls, tmp_path):
    import asyncio
    from app.skills.test_generator_skill import TestGeneratorSkill

    source = tmp_path / "mymodule.py"
    source.write_text(
        "def add(x, y):\n    '''Add two numbers.'''\n    return x + y\n\n"
        "def subtract(x, y):\n    return x - y\n"
    )
    (tmp_path / "tests").mkdir()

    test_content = (
        "import pytest\nfrom mymodule import add, subtract\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_subtract():\n    assert subtract(5, 3) == 2\n"
    )
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=test_content)]
    mock_client.messages.create.return_value = mock_msg

    with patch("app.skills.test_generator_skill.shell_run") as mock_shell:
        from app.utils.shell import ShellResult
        mock_shell.return_value = ShellResult(returncode=0, stdout="2 tests collected\n", stderr="")

        skill = TestGeneratorSkill()
        result = asyncio.run(
            skill.execute(
                {"source_path": "mymodule.py", "repo_path": str(tmp_path)},
                "generate tests",
            )
        )

    assert "test_mymodule_generated.py" in result.context_data
    assert (tmp_path / "tests" / "test_mymodule_generated.py").exists()


# ── DeployVerifierSkill ───────────────────────────────────────────────────────

@patch("app.skills.deploy_verifier_skill.post_alert_sync")
def test_deploy_verifier_polls_health_endpoint(mock_slack):
    import asyncio
    from app.skills.deploy_verifier_skill import DeployVerifierSkill

    call_count = [0]

    def make_resp(status):
        resp = MagicMock()
        resp.status = status
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    def mock_get(*args, **kwargs):
        call_count[0] += 1
        return make_resp(200 if call_count[0] >= 3 else 503)

    async def fake_sleep(_):
        pass

    with patch("app.skills.deploy_verifier_skill.aiohttp") as mock_aiohttp:
        mock_session = MagicMock()
        mock_session.get = mock_get
        mock_aiohttp.ClientSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_aiohttp.ClientSession.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_aiohttp.ClientTimeout = MagicMock(return_value=MagicMock())

        with patch("app.skills.deploy_verifier_skill.asyncio.sleep", side_effect=fake_sleep):
            skill = DeployVerifierSkill()
            result = asyncio.run(
                skill.execute({"base_url": "http://localhost:8000"}, "verify deploy")
            )

    data = json.loads(result.context_data)
    assert data["ok"] is True
    mock_slack.assert_called_once()


@patch("app.skills.deploy_verifier_skill.post_alert_sync")
@patch("app.skills.deploy_verifier_skill.DeployVerifierSkill._trigger_rollback")
def test_deploy_verifier_triggers_rollback_on_health_fail(mock_rollback, mock_slack):
    import asyncio
    from app.skills.deploy_verifier_skill import DeployVerifierSkill

    async def mock_rollback_async(*args, **kwargs):
        return "Rollback triggered."

    mock_rollback.side_effect = mock_rollback_async

    def make_resp_500():
        resp = MagicMock()
        resp.status = 500
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    async def fake_sleep(_):
        pass

    with patch("app.skills.deploy_verifier_skill.aiohttp") as mock_aiohttp:
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=make_resp_500())
        mock_aiohttp.ClientSession.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_aiohttp.ClientSession.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_aiohttp.ClientTimeout = MagicMock(return_value=MagicMock())

        with patch("app.skills.deploy_verifier_skill.asyncio.sleep", side_effect=fake_sleep):
            skill = DeployVerifierSkill()
            result = asyncio.run(
                skill.execute({"base_url": "http://localhost:8000"}, "verify deploy")
            )

    assert result.is_error
    data = json.loads(result.context_data)
    assert data["ok"] is False
    mock_rollback.assert_called_once()
    mock_slack.assert_called_once()
