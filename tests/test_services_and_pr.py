"""
Tests for:
- app/services/log_monitor.py (_classify_line, get_service_health, _poll)
- app/services/error_middleware.py (ErrorCollectionMiddleware)
- app/worker/pr_tasks.py (_gh_headers, _gh_get/_post/_put, _resolve_conflict, _list_open_prs)
- app/skills/cicd_debug.py (error paths that don't require live GitHub)
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Stub modules unavailable locally
_celery_mock = MagicMock()
_celery_mock.shared_task = lambda **kw: (lambda f: f)
_celery_schedules_mock = MagicMock()
_celery_schedules_mock.crontab = MagicMock
for _mod, _obj in [
    ("tenacity", MagicMock()),
    ("celery", _celery_mock),
    ("celery.app", MagicMock()),
    ("celery.schedules", _celery_schedules_mock),
    ("kombu", MagicMock()),
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = _obj


# ── services/log_monitor.py ───────────────────────────────────────────────────


def test_classify_line_traceback():
    from app.services.log_monitor import _classify_line
    assert _classify_line("Traceback (most recent call last):") == "exception"


def test_classify_line_exception():
    from app.services.log_monitor import _classify_line
    assert _classify_line("AttributeError: NoneType exception") == "exception"


def test_classify_line_critical():
    from app.services.log_monitor import _classify_line
    assert _classify_line("CRITICAL: database connection lost") == "critical"


def test_classify_line_warning():
    from app.services.log_monitor import _classify_line
    assert _classify_line("WARNING: disk usage at 92%") == "warning"


def test_classify_line_warn_variant():
    from app.services.log_monitor import _classify_line
    assert _classify_line("WARN slow query detected") == "warning"


def test_classify_line_error():
    from app.services.log_monitor import _classify_line
    assert _classify_line("error connecting to Redis") == "error"


async def test_log_monitor_get_service_health_empty():
    from app.services.log_monitor import LogMonitor
    monitor = LogMonitor()
    # Empty buffer
    with patch("app.services.log_monitor.error_collector") as mock_ec:
        mock_ec.error_buffer = []
        health = await monitor.get_service_health()
    assert health == {}


async def test_log_monitor_get_service_health_with_errors():
    from app.services.log_monitor import LogMonitor
    monitor = LogMonitor()
    with patch("app.services.log_monitor.error_collector") as mock_ec:
        mock_ec.error_buffer = [
            {"service": "brain", "error_type": "exception"},
            {"service": "brain", "error_type": "warning"},
            {"service": "celery", "error_type": "error"},
        ]
        health = await monitor.get_service_health()
    assert health["brain"]["error_count"] == 2
    assert health["celery"]["error_count"] == 1


async def test_log_monitor_poll_loki_http_error():
    from app.services.log_monitor import LogMonitor
    monitor = LogMonitor()
    monitor._last_ts_ns = 0
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    with patch("httpx.AsyncClient") as MockHTTP:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=mock_resp)))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        MockHTTP.return_value = mock_ctx
        # Should not raise
        await monitor._poll()


async def test_log_monitor_poll_loki_connection_error():
    from app.services.log_monitor import LogMonitor
    import httpx
    monitor = LogMonitor()
    monitor._last_ts_ns = 0
    with patch("httpx.AsyncClient") as MockHTTP:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        MockHTTP.return_value = mock_ctx
        await monitor._poll()  # Should not raise


async def test_log_monitor_poll_no_results():
    from app.services.log_monitor import LogMonitor
    monitor = LogMonitor()
    monitor._last_ts_ns = 0
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"result": []}}
    with patch("httpx.AsyncClient") as MockHTTP:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        MockHTTP.return_value = mock_ctx
        await monitor._poll()  # Should not raise


async def test_log_monitor_poll_with_data():
    from app.services.log_monitor import LogMonitor
    monitor = LogMonitor()
    monitor._last_ts_ns = 0
    loki_data = {
        "data": {
            "result": [{
                "stream": {"container_name": "brain"},
                "values": [
                    ["1700000000000000000", "ERROR: connection failed"],
                    ["1700000001000000000", "Traceback (most recent call last):"],
                ]
            }]
        }
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = loki_data
    with patch("httpx.AsyncClient") as MockHTTP, \
         patch("app.services.log_monitor.error_collector") as mock_ec:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        MockHTTP.return_value = mock_ctx
        mock_ec.log_error = AsyncMock(return_value=True)
        await monitor._poll()
    mock_ec.log_error.assert_called()


# ── services/error_middleware.py ──────────────────────────────────────────────


async def test_error_middleware_passes_through_200():
    from app.services.error_middleware import ErrorCollectionMiddleware
    middleware = ErrorCollectionMiddleware(app=MagicMock())
    mock_request = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    call_next = AsyncMock(return_value=mock_response)
    result = await middleware.dispatch(mock_request, call_next)
    assert result == mock_response


async def test_error_middleware_logs_500():
    from app.services.error_middleware import ErrorCollectionMiddleware
    middleware = ErrorCollectionMiddleware(app=MagicMock())
    mock_request = MagicMock()
    mock_request.method = "GET"
    mock_request.url.path = "/api/v1/chat"
    mock_request.client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    call_next = AsyncMock(return_value=mock_response)
    with patch("app.services.error_middleware.error_collector") as mock_ec:
        mock_ec.log_error = AsyncMock(return_value=False)
        result = await middleware.dispatch(mock_request, call_next)
    mock_ec.log_error.assert_called_once()


async def test_error_middleware_handles_exception():
    from app.services.error_middleware import ErrorCollectionMiddleware
    from starlette.responses import JSONResponse
    middleware = ErrorCollectionMiddleware(app=MagicMock())
    mock_request = MagicMock()
    mock_request.method = "POST"
    mock_request.url.path = "/api/v1/chat"
    mock_request.client = MagicMock()
    call_next = AsyncMock(side_effect=RuntimeError("unexpected error"))
    with patch("app.services.error_middleware.error_collector") as mock_ec:
        mock_ec.log_error = AsyncMock(return_value=False)
        result = await middleware.dispatch(mock_request, call_next)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500


# ── worker/pr_tasks.py pure helpers ───────────────────────────────────────────


def test_gh_headers_structure():
    from app.worker.pr_tasks import _gh_headers
    with patch("app.worker.pr_tasks.settings") as mock_settings:
        mock_settings.github_token = "ghp_testtoken"
        headers = _gh_headers()
    assert "Authorization" in headers
    assert "ghp_testtoken" in headers["Authorization"]
    assert headers["Accept"] == "application/vnd.github+json"


def test_gh_get_success():
    from app.worker.pr_tasks import _gh_get
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"number": 1, "title": "Test PR"}
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_client)
    mock_ctx.__exit__ = MagicMock(return_value=None)
    with patch("httpx.Client", return_value=mock_ctx), \
         patch("app.worker.pr_tasks.settings") as mock_settings:
        mock_settings.github_token = "ghp_test"
        result = _gh_get("/repos/org/repo/pulls/1")
    assert result["number"] == 1


def test_gh_post_success():
    from app.worker.pr_tasks import _gh_post
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": 100}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_client)
    mock_ctx.__exit__ = MagicMock(return_value=None)
    with patch("httpx.Client", return_value=mock_ctx), \
         patch("app.worker.pr_tasks.settings") as mock_settings:
        mock_settings.github_token = "ghp_test"
        result = _gh_post("/repos/org/repo/issues/1/comments", {"body": "LGTM"})
    assert result["id"] == 100


def test_gh_put_success():
    from app.worker.pr_tasks import _gh_put
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"merged": True}
    mock_client = MagicMock()
    mock_client.put.return_value = mock_resp
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_client)
    mock_ctx.__exit__ = MagicMock(return_value=None)
    with patch("httpx.Client", return_value=mock_ctx), \
         patch("app.worker.pr_tasks.settings") as mock_settings:
        mock_settings.github_token = "ghp_test"
        result = _gh_put("/repos/org/repo/pulls/1/merge", {"merge_method": "squash"})
    assert result["merged"] is True


def test_list_open_sentinel_prs_no_config():
    from app.worker.pr_tasks import _list_open_sentinel_prs
    with patch("app.worker.pr_tasks.settings") as mock_settings:
        mock_settings.github_default_repo = ""
        mock_settings.github_token = ""
        result = _list_open_sentinel_prs()
    assert result == []


def test_list_open_sentinel_prs_with_prs():
    from app.worker.pr_tasks import _list_open_sentinel_prs
    mock_prs = [
        {"number": 5, "head": {"ref": "sentinel/task-123-20260101"}},
        {"number": 6, "head": {"ref": "feature/my-feature"}},  # not sentinel
        {"number": 7, "head": {"ref": "sentinel/pr-auto-review"}},
    ]
    with patch("app.worker.pr_tasks.settings") as mock_settings, \
         patch("app.worker.pr_tasks._gh_get", return_value=mock_prs):
        mock_settings.github_default_repo = "org/repo"
        mock_settings.github_token = "ghp_test"
        result = _list_open_sentinel_prs()
    assert 5 in result
    assert 7 in result
    assert 6 not in result  # not a sentinel branch


def test_list_open_sentinel_prs_exception():
    from app.worker.pr_tasks import _list_open_sentinel_prs
    with patch("app.worker.pr_tasks.settings") as mock_settings, \
         patch("app.worker.pr_tasks._gh_get", side_effect=Exception("API error")):
        mock_settings.github_default_repo = "org/repo"
        mock_settings.github_token = "ghp_test"
        result = _list_open_sentinel_prs()
    assert result == []


def test_resolve_conflict_cannot_resolve():
    from app.worker.pr_tasks import _resolve_conflict_with_llm
    mock_msg = MagicMock()
    mock_msg.content[0].text = "CANNOT_RESOLVE"
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = mock_msg
    with patch("app.worker.pr_tasks.settings") as mock_settings, \
         patch("anthropic.Anthropic", return_value=mock_anthropic):
        mock_settings.anthropic_api_key = "sk-ant-test"
        result = _resolve_conflict_with_llm("app/main.py", "<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>>")
    assert result is None


def test_resolve_conflict_returns_resolved():
    from app.worker.pr_tasks import _resolve_conflict_with_llm
    mock_msg = MagicMock()
    mock_msg.content[0].text = "def main():\n    pass\n"
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = mock_msg
    with patch("app.worker.pr_tasks.settings") as mock_settings, \
         patch("anthropic.Anthropic", return_value=mock_anthropic):
        mock_settings.anthropic_api_key = "sk-ant-test"
        result = _resolve_conflict_with_llm("app/main.py", "conflicted content")
    assert "def main():" in result and "pass" in result


# ── skills/cicd_debug.py early-exit paths ────────────────────────────────────


async def test_cicd_debug_no_repo():
    from app.skills.cicd_debug import CicdDebugSkill
    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value.github_default_repo = ""
        mock_settings.return_value.github_token = "ghp_test"
        r = await CicdDebugSkill().execute({}, "debug CI")
    assert "cicd_debug needs a repo" in r.context_data


async def test_cicd_debug_no_token():
    from app.skills.cicd_debug import CicdDebugSkill
    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value.github_default_repo = "org/repo"
        mock_settings.return_value.github_token = ""
        r = await CicdDebugSkill().execute({}, "debug CI")
    assert "GITHUB_TOKEN" in r.context_data


def test_cicd_debug_metadata():
    from app.skills.cicd_debug import CicdDebugSkill
    from app.skills.base import ApprovalCategory
    s = CicdDebugSkill()
    assert s.name == "cicd_debug"
    assert s.approval_category == ApprovalCategory.NONE
    assert s.is_available() is True
