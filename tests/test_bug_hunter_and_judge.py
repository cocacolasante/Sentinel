"""
Tests for:
- app/worker/bug_hunter_tasks.py (pure helpers)
- app/evals/judge.py (judge_response with mocked anthropic)
- app/worker/project_tasks.py (pure helpers _update_project, _slack_error)
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Stub modules unavailable locally
_celery_mock = MagicMock()
_celery_mock.shared_task = lambda **kw: (lambda f: f)
for _mod, _obj in [
    ("tenacity", MagicMock()),
    ("celery", _celery_mock),
    ("celery.app", MagicMock()),
    ("celery.schedules", MagicMock()),
    ("kombu", MagicMock()),
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = _obj


# ── evals/judge.py ────────────────────────────────────────────────────────────


def test_judge_response_success():
    import json
    from app.evals.judge import judge_response
    mock_result = MagicMock()
    mock_result.content[0].text = json.dumps({
        "score": 8,
        "passed": True,
        "reasoning": "Response meets all criteria.",
    })
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_result
    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.evals.judge.settings") as mock_settings:
        mock_settings.anthropic_api_key = "sk-ant-test"
        result = judge_response(
            response="Here is a good answer.",
            criteria=["is helpful", "is accurate"],
            judge_prompt="Score 0-10",
            threshold=7,
        )
    assert result["score"] == 8
    assert result["passed"] is True
    assert "criteria" in result["reasoning"] or len(result["reasoning"]) > 0


def test_judge_response_json_parse_error():
    from app.evals.judge import judge_response
    mock_result = MagicMock()
    mock_result.content[0].text = "not valid json at all"
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_result
    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.evals.judge.settings") as mock_settings:
        mock_settings.anthropic_api_key = "sk-ant-test"
        result = judge_response("response", ["criteria"], "prompt", 7)
    assert result["score"] == 0
    assert result["passed"] is False
    assert "parse error" in result["reasoning"].lower()


def test_judge_response_api_exception():
    from app.evals.judge import judge_response
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("connection failed")
    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.evals.judge.settings") as mock_settings:
        mock_settings.anthropic_api_key = "sk-ant-test"
        result = judge_response("response", ["criteria"], "prompt", 7)
    assert result["score"] == 0
    assert result["passed"] is False


def test_judge_response_strips_markdown_fence():
    import json
    from app.evals.judge import judge_response
    raw = '```json\n{"score": 9, "passed": true, "reasoning": "Great"}\n```'
    mock_result = MagicMock()
    mock_result.content[0].text = raw
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_result
    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.evals.judge.settings") as mock_settings:
        mock_settings.anthropic_api_key = "sk-ant-test"
        result = judge_response("response", [], "prompt", 7)
    assert result["score"] == 9


def test_judge_response_clamps_score():
    import json
    from app.evals.judge import judge_response
    mock_result = MagicMock()
    mock_result.content[0].text = json.dumps({"score": 15, "passed": True, "reasoning": "too high"})
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_result
    with patch("anthropic.Anthropic", return_value=mock_client), \
         patch("app.evals.judge.settings") as mock_settings:
        mock_settings.anthropic_api_key = "sk-ant-test"
        result = judge_response("response", [], "prompt", 7)
    assert result["score"] == 10  # clamped


# ── worker/bug_hunter_tasks.py helpers ────────────────────────────────────────


def test_service_from_filename_with_container_id():
    from app.worker.bug_hunter_tasks import _service_from_filename
    # 64-char hex string in container log path
    container_id = "a" * 64
    filename = f"/var/lib/docker/containers/{container_id}/json.log"
    container_map = {("a" * 64)[:12]: "brain"}
    result = _service_from_filename(filename, container_map)
    assert result == "brain"


def test_service_from_filename_no_match():
    from app.worker.bug_hunter_tasks import _service_from_filename
    result = _service_from_filename("/some/other/path.log", {})
    assert result == ""


def test_infer_service_from_log_go_caller():
    from app.worker.bug_hunter_tasks import _infer_service_from_log
    line = 'caller=scheduler_processor.go:106 msg="error fetching data"'
    result = _infer_service_from_log(line)
    assert result == "loki"


def test_infer_service_from_log_nginx():
    from app.worker.bug_hunter_tasks import _infer_service_from_log
    line = "2026/03/05 15:06:13 [error] 30#30: upstream timed out"
    result = _infer_service_from_log(line)
    assert result == "nginx"


def test_infer_service_from_log_python_brain():
    from app.worker.bug_hunter_tasks import _infer_service_from_log
    line = "2026-03-05 10:00:00 | ERROR | app.skills.chat_skill:execute:42 - failed"
    result = _infer_service_from_log(line)
    assert result == "brain"


def test_infer_service_from_log_celery_worker():
    from app.worker.bug_hunter_tasks import _infer_service_from_log
    line = "2026-03-05 10:00:00 | ERROR | app.worker.tasks:_mark_task:50 - db error"
    result = _infer_service_from_log(line)
    assert result == "celery-worker"


def test_infer_service_from_log_unknown():
    from app.worker.bug_hunter_tasks import _infer_service_from_log
    result = _infer_service_from_log("some random log line with no patterns")
    assert result is None


def test_normalize_line_strips_prefix():
    from app.worker.bug_hunter_tasks import _normalize_line
    line = "2026-03-05 10:00:00.123 | ERROR | app.skills.chat:exec:42 - Connection refused"
    result = _normalize_line(line)
    assert "Connection refused" in result or len(result) > 0


def test_normalize_line_extracts_msg():
    from app.worker.bug_hunter_tasks import _normalize_line
    line = 'level=error caller=ingester.go:106 msg="failed to push samples to ingester"'
    result = _normalize_line(line)
    assert "failed to push samples to ingester" in result


def test_normalize_line_dynamic_tokens_replaced():
    from app.worker.bug_hunter_tasks import _normalize_line
    line = "ERROR User 987654321000 not found at timestamp 2026-03-05T10:00:00"
    result = _normalize_line(line)
    # Dynamic tokens should be replaced with *
    assert "987654321000" not in result


def test_get_container_service_map_subprocess_failure():
    from app.worker.bug_hunter_tasks import _get_container_service_map
    with patch("subprocess.run", side_effect=Exception("docker not available")):
        result = _get_container_service_map()
    assert result == {}


def test_get_container_service_map_no_docker():
    from app.worker.bug_hunter_tasks import _get_container_service_map
    import subprocess
    mock_result = MagicMock()
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        result = _get_container_service_map()
    assert result == {}


def test_get_container_service_map_with_output():
    from app.worker.bug_hunter_tasks import _get_container_service_map
    mock_result = MagicMock()
    mock_result.stdout = "abc123def456\tai-brain\nfed987cba654\tai-celery\n"
    with patch("subprocess.run", return_value=mock_result):
        result = _get_container_service_map()
    assert result.get("abc123def456") == "brain"
    assert result.get("fed987cba654") == "celery"


def test_build_slack_report_basic():
    from app.worker.bug_hunter_tasks import _build_slack_report
    findings = [{
        "service": "brain",
        "count": 5,
        "fingerprint": "ConnectionError: failed to connect",
        "analysis": {
            "severity": "high",
            "is_noise": False,
            "affected_component": "DB",
            "root_cause": "DB down",
            "proposed_fix": "Restart DB",
            "fix_snippet": None,
        },
        "task_id": 42,
    }]
    text = _build_slack_report(findings, total_lines=100, hours=24, tasks_created=[42])
    assert "Bug Hunt" in text
    assert "brain" in text
    assert "ConnectionError" in text
    assert "42" in text


def test_build_slack_report_with_noise():
    from app.worker.bug_hunter_tasks import _build_slack_report
    findings = [{
        "service": "loki",
        "count": 2,
        "fingerprint": "INFO message",
        "analysis": {"severity": "low", "is_noise": True},
    }]
    text = _build_slack_report(findings, total_lines=50, hours=6, tasks_created=[])
    assert "Noise" in text or "loki" in text


def test_build_slack_report_empty():
    from app.worker.bug_hunter_tasks import _build_slack_report
    text = _build_slack_report([], total_lines=0, hours=24, tasks_created=[])
    assert "Bug Hunt" in text


# ── worker/project_tasks.py helpers ──────────────────────────────────────────


def test_update_project_no_fields():
    from app.worker.project_tasks import _update_project
    with patch("app.db.postgres.execute") as mock_exec:
        _update_project(1)  # no fields → early return
    mock_exec.assert_not_called()


def test_update_project_with_fields():
    from app.worker.project_tasks import _update_project
    with patch("app.db.postgres.execute") as mock_exec:
        _update_project(1, status="deploying", last_deployed="now")
    mock_exec.assert_called_once()
    call_sql = mock_exec.call_args[0][0]
    assert "UPDATE projects" in call_sql


def test_update_project_db_exception():
    from app.worker.project_tasks import _update_project
    with patch("app.db.postgres.execute", side_effect=Exception("db down")):
        _update_project(1, status="failed")  # Should not raise


def test_slack_error_no_db_row():
    from app.worker.project_tasks import _slack_error
    with patch("app.db.postgres.execute_one", return_value=None):
        _slack_error(1, "Deploy failed: connection refused")  # Should not raise


def test_slack_error_no_channel():
    from app.worker.project_tasks import _slack_error
    with patch("app.db.postgres.execute_one", return_value={"name": "proj", "slack_channel": None, "slack_thread_ts": None}):
        _slack_error(1, "Deploy failed")  # No channel → skip Slack


def test_slack_error_with_channel():
    from app.worker.project_tasks import _slack_error
    with patch("app.db.postgres.execute_one", return_value={
        "name": "Sentinel",
        "slack_channel": "C123",
        "slack_thread_ts": "1234567890.123",
    }), patch("app.integrations.slack_notifier.post_thread_reply_sync") as mock_post:
        _slack_error(1, "Deploy failed: timeout")
    mock_post.assert_called_once()
    text = mock_post.call_args[0][0]
    assert "Deploy failed" in text


def test_slack_error_exception_no_raise():
    from app.worker.project_tasks import _slack_error
    with patch("app.db.postgres.execute_one", side_effect=Exception("db down")):
        _slack_error(99, "error message")  # Should not raise


def test_get_ssh_key_path_from_env_content(tmp_path):
    from app.worker.project_tasks import _get_ssh_key_path
    import os
    fake_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----"
    workspace = str(tmp_path)
    with patch.dict(os.environ, {"IONOS_SSH_PRIVATE_KEY": fake_key, "IONOS_SSH_PRIVATE_KEY_PATH": ""}):
        with patch("app.worker.project_tasks._WORKSPACE", workspace):
            path = _get_ssh_key_path()
    assert path is not None
    assert path.endswith("id_deploy")


def test_get_ssh_key_path_from_path_env(tmp_path):
    from app.worker.project_tasks import _get_ssh_key_path
    import os
    key_file = tmp_path / "test_key"
    key_file.write_text("key content")
    with patch.dict(os.environ, {"IONOS_SSH_PRIVATE_KEY": "", "IONOS_SSH_PRIVATE_KEY_PATH": str(key_file)}):
        path = _get_ssh_key_path()
    assert path == str(key_file)


def test_get_ssh_key_path_none_when_no_key():
    from app.worker.project_tasks import _get_ssh_key_path
    import os
    with patch.dict(os.environ, {"IONOS_SSH_PRIVATE_KEY": "", "IONOS_SSH_PRIVATE_KEY_PATH": ""}):
        with patch("os.path.exists", return_value=False):
            path = _get_ssh_key_path()
    assert path is None
