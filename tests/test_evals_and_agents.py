"""
Tests for evals/base (already in test_hooks_and_services), evals/reporter,
evals/integrations, agents/base, agents/definitions, agents/registry.

All external calls (Slack, DB, integrations) are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


# ── agents/base.py ────────────────────────────────────────────────────────────


def test_agent_dataclass_fields():
    from app.agents.base import Agent
    a = Agent(
        name="engineer",
        display_name="Engineer",
        system_prompt="You are an engineer.",
        preferred_model="claude-sonnet-4-6",
        max_tokens=8096,
        temperature=1.0,
        trigger_intents=["code"],
        trigger_keywords=["python", "bug"],
    )
    assert a.name == "engineer"
    assert a.preferred_model == "claude-sonnet-4-6"
    assert "code" in a.trigger_intents
    assert "python" in a.trigger_keywords


def test_agent_defaults():
    from app.agents.base import Agent
    a = Agent(name="test", display_name="Test", system_prompt="prompt")
    assert a.preferred_model == "claude-sonnet-4-6"
    assert a.max_tokens == 2048
    assert a.temperature == 1.0
    assert a.trigger_intents == []
    assert a.trigger_keywords == []


# ── agents/definitions.py ─────────────────────────────────────────────────────


def test_default_agent_exists():
    from app.agents.definitions import DEFAULT_AGENT
    assert DEFAULT_AGENT.name == "default"


def test_engineer_agent_exists():
    from app.agents.definitions import ENGINEER_AGENT
    assert ENGINEER_AGENT.name == "engineer"
    assert "code" in ENGINEER_AGENT.trigger_intents


def test_writer_agent_exists():
    from app.agents.definitions import WRITER_AGENT
    assert WRITER_AGENT.name == "writer"


def test_researcher_agent_exists():
    from app.agents.definitions import RESEARCHER_AGENT
    assert RESEARCHER_AGENT.name == "researcher"


def test_strategist_agent_exists():
    from app.agents.definitions import STRATEGIST_AGENT
    assert STRATEGIST_AGENT.name == "strategist"


def test_marketing_agent_exists():
    from app.agents.definitions import MARKETING_AGENT
    assert MARKETING_AGENT.name == "marketing"


# ── agents/registry.py ────────────────────────────────────────────────────────


def test_agent_registry_loads_defaults():
    from app.agents.registry import AgentRegistry
    reg = AgentRegistry()
    agents = reg.list_agents()
    names = [a["name"] for a in agents]
    assert "engineer" in names
    assert "default" in names


def test_agent_registry_select_by_intent():
    from app.agents.registry import AgentRegistry
    reg = AgentRegistry()
    agent = reg.select("code", "write me some python")
    assert agent.name == "engineer"


def test_agent_registry_select_by_keyword():
    from app.agents.registry import AgentRegistry
    reg = AgentRegistry()
    agent = reg.select("unknown_intent", "write me a blog post about AI")
    # Should match writer by keyword "write"
    assert agent is not None


def test_agent_registry_select_falls_back_to_default():
    from app.agents.registry import AgentRegistry
    reg = AgentRegistry()
    agent = reg.select("zzz_no_match", "zzz random gibberish zyx")
    assert agent.name == "default"


def test_agent_registry_get_by_name():
    from app.agents.registry import AgentRegistry
    reg = AgentRegistry()
    a = reg.get("engineer")
    assert a is not None
    assert a.name == "engineer"


def test_agent_registry_get_missing():
    from app.agents.registry import AgentRegistry
    reg = AgentRegistry()
    assert reg.get("nonexistent") is None


def test_agent_registry_list_agents_structure():
    from app.agents.registry import AgentRegistry
    reg = AgentRegistry()
    agents = reg.list_agents()
    assert all("name" in a and "display_name" in a for a in agents)


# ── evals/reporter.py — format_scorecard (pure) ───────────────────────────────


def _make_summary(name, score, pass_rate, total, passed, results=None):
    from app.evals.base import AgentEvalSummary
    return AgentEvalSummary(
        agent_name=name, run_id="r1",
        avg_score=score, pass_rate=pass_rate,
        total_tests=total, passed_tests=passed,
        results=results or [],
    )


def test_format_scorecard_basic():
    from app.evals.reporter import format_scorecard
    summaries = [
        _make_summary("engineer", 8.5, 1.0, 3, 3),
        _make_summary("writer", 6.0, 0.67, 3, 2),
    ]
    text = format_scorecard(summaries)
    assert "Weekly Brain Eval Report" in text
    assert "engineer" in text.lower() or "Engineer" in text
    assert "writer" in text.lower() or "Writer" in text


def test_format_scorecard_with_previous_scores():
    from app.evals.reporter import format_scorecard
    summaries = [_make_summary("engineer", 8.5, 1.0, 3, 3)]
    text = format_scorecard(summaries, previous_scores={"engineer": 8.0})
    assert "+0.5" in text or "vs last week" in text


def test_format_scorecard_degraded_flagged():
    from app.evals.reporter import format_scorecard
    summaries = [_make_summary("researcher", 5.0, 0.33, 3, 1)]
    text = format_scorecard(summaries, previous_scores={"researcher": 8.0})
    assert "degraded" in text


def test_format_scorecard_baseline_when_no_previous():
    from app.evals.reporter import format_scorecard
    summaries = [_make_summary("strategist", 7.5, 1.0, 3, 3)]
    text = format_scorecard(summaries, previous_scores=None)
    assert "baseline" in text


def test_format_scorecard_empty_summaries():
    from app.evals.reporter import format_scorecard
    text = format_scorecard([])
    assert "Weekly Brain Eval Report" in text


def test_format_scorecard_failed_tests_section():
    from app.evals.base import EvalResult
    from app.evals.reporter import format_scorecard
    failed_result = EvalResult(
        run_id="r1", agent_name="writer", test_name="blog_post",
        input="write a blog", response="meh", score=4.0,
        threshold=7, passed=False, reasoning="not good enough", latency_ms=100.0,
    )
    summaries = [_make_summary("writer", 4.0, 0.0, 3, 0, results=[failed_result])]
    text = format_scorecard(summaries)
    assert "Failed tests" in text


def test_format_scorecard_run_id_in_footer():
    from app.evals.reporter import format_scorecard
    summaries = [_make_summary("engineer", 9.0, 1.0, 3, 3)]
    summaries[0].run_id = "abc123"
    text = format_scorecard(summaries)
    assert "abc123" in text


def test_format_scorecard_no_integration_results():
    from app.evals.reporter import format_scorecard
    summaries = [_make_summary("engineer", 8.0, 1.0, 3, 3)]
    text = format_scorecard(summaries, integration_results=None)
    assert "Integration uptime" not in text


def test_format_scorecard_with_integration_results():
    from app.evals.base import IntegrationEvalResult
    from app.evals.reporter import format_scorecard
    int_results = [
        IntegrationEvalResult("gmail", True, 100.0, None),
        IntegrationEvalResult("github", False, None, "timeout"),
    ]
    with patch("app.evals.integrations.get_uptime_pct", return_value=None):
        text = format_scorecard(
            [_make_summary("engineer", 8.0, 1.0, 3, 3)],
            integration_results=int_results,
        )
    assert "Integration uptime" in text


# ── evals/reporter.py — post_integration_health_to_slack ─────────────────────


async def test_post_integration_health_no_results():
    from app.evals.reporter import post_integration_health_to_slack
    result = await post_integration_health_to_slack([])
    assert result is True


async def test_post_integration_health_all_passed():
    from app.evals.base import IntegrationEvalResult
    from app.evals.reporter import post_integration_health_to_slack
    results = [
        IntegrationEvalResult("gmail", True, 50.0, None),
        IntegrationEvalResult("github", True, 80.0, None),
    ]
    mock_client = MagicMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})
    mock_cls = MagicMock(return_value=mock_client)
    with patch.dict("sys.modules", {"slack_sdk": MagicMock(), "slack_sdk.web": MagicMock(), "slack_sdk.web.async_client": MagicMock(AsyncWebClient=mock_cls)}):
        r = await post_integration_health_to_slack(results, channel="sentinel-evals")
    assert r is True


async def test_post_integration_health_with_failures():
    from app.evals.base import IntegrationEvalResult
    from app.evals.reporter import post_integration_health_to_slack
    results = [
        IntegrationEvalResult("gmail", True, 50.0, None),
        IntegrationEvalResult("n8n", False, None, "connection refused"),
    ]
    mock_client = MagicMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})
    mock_cls = MagicMock(return_value=mock_client)
    with patch.dict("sys.modules", {"slack_sdk": MagicMock(), "slack_sdk.web": MagicMock(), "slack_sdk.web.async_client": MagicMock(AsyncWebClient=mock_cls)}):
        r = await post_integration_health_to_slack(results, channel="sentinel-alerts")
    assert r is True


async def test_post_integration_health_slack_exception():
    from app.evals.base import IntegrationEvalResult
    from app.evals.reporter import post_integration_health_to_slack
    results = [IntegrationEvalResult("github", False, None, "timeout")]
    with patch.dict("sys.modules", {"slack_sdk": MagicMock(), "slack_sdk.web": MagicMock(), "slack_sdk.web.async_client": MagicMock(AsyncWebClient=MagicMock(side_effect=Exception("no token")))}):
        r = await post_integration_health_to_slack(results)
    assert r is False


# ── evals/reporter.py — post_scorecard_to_slack ───────────────────────────────


async def test_post_scorecard_no_token():
    from app.evals.reporter import post_scorecard_to_slack
    with patch("app.evals.reporter.settings") as mock_settings:
        mock_settings.slack_bot_token = ""
        r = await post_scorecard_to_slack([])
    assert r is False


async def test_post_scorecard_success():
    from app.evals.reporter import post_scorecard_to_slack
    summaries = [_make_summary("engineer", 8.0, 1.0, 3, 3)]
    mock_client = MagicMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})
    mock_cls = MagicMock(return_value=mock_client)
    with patch("app.evals.reporter.settings") as mock_settings:
        mock_settings.slack_bot_token = "xoxb-test"
        mock_settings.slack_eval_channel = "sentinel-evals"
        with patch.dict("sys.modules", {"slack_sdk": MagicMock(), "slack_sdk.web": MagicMock(), "slack_sdk.web.async_client": MagicMock(AsyncWebClient=mock_cls)}):
            r = await post_scorecard_to_slack(summaries)
    assert r is True


async def test_post_scorecard_slack_failure():
    from app.evals.reporter import post_scorecard_to_slack
    summaries = [_make_summary("engineer", 8.0, 1.0, 3, 3)]
    mock_client = MagicMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ok": False, "error": "channel_not_found"})
    mock_cls = MagicMock(return_value=mock_client)
    with patch("app.evals.reporter.settings") as mock_settings:
        mock_settings.slack_bot_token = "xoxb-test"
        mock_settings.slack_eval_channel = "sentinel-evals"
        with patch.dict("sys.modules", {"slack_sdk": MagicMock(), "slack_sdk.web": MagicMock(), "slack_sdk.web.async_client": MagicMock(AsyncWebClient=mock_cls)}):
            r = await post_scorecard_to_slack(summaries)
    assert r is False


# ── evals/integrations.py ─────────────────────────────────────────────────────


async def test_check_gmail_not_configured():
    from app.evals.integrations import _check_gmail
    with patch("app.integrations.gmail.GmailClient") as MockGmail:
        inst = MockGmail.return_value
        inst.is_configured.return_value = False
        result = await _check_gmail()
    assert result.integration == "gmail"
    assert result.passed is False
    assert "Not configured" in (result.error or "")


async def test_check_github_exception():
    from app.evals.integrations import _check_github
    with patch("app.integrations.github.GitHubClient", side_effect=Exception("import fail")):
        result = await _check_github()
    assert result.integration == "github"
    assert result.passed is False


async def test_check_n8n_not_configured():
    from app.evals.integrations import _check_n8n
    with patch("app.integrations.n8n_bridge.N8nBridge") as MockN8n:
        inst = MockN8n.return_value
        inst.is_configured.return_value = False
        result = await _check_n8n()
    assert result.passed is False


async def test_check_home_assistant_not_configured():
    from app.evals.integrations import _check_home_assistant
    with patch("app.integrations.home_assistant.HomeAssistantClient") as MockHA:
        inst = MockHA.return_value
        inst.is_configured.return_value = False
        result = await _check_home_assistant()
    assert result.passed is False


def test_persist_integration_results_no_crash():
    from app.evals.integrations import _persist_integration_results
    from app.evals.base import IntegrationEvalResult
    results = [
        IntegrationEvalResult("gmail", True, 50.0, None),
        IntegrationEvalResult("github", False, None, "timeout"),
    ]
    with patch("app.db.postgres.execute"):
        _persist_integration_results(results)  # Should not raise


def test_persist_integration_results_db_exception():
    from app.evals.integrations import _persist_integration_results
    from app.evals.base import IntegrationEvalResult
    results = [IntegrationEvalResult("gmail", True, 50.0, None)]
    with patch("app.db.postgres.execute", side_effect=Exception("db down")):
        _persist_integration_results(results)  # Should not raise


def test_get_uptime_pct_returns_value():
    from app.evals.integrations import get_uptime_pct
    with patch("app.db.postgres.execute_one", return_value={"passed_count": 7, "total_count": 10}):
        pct = get_uptime_pct("gmail", days=7)
    assert pct == 70.0


def test_get_uptime_pct_no_data():
    from app.evals.integrations import get_uptime_pct
    with patch("app.db.postgres.execute_one", return_value={"passed_count": 0, "total_count": 0}):
        pct = get_uptime_pct("gmail")
    assert pct is None


def test_get_uptime_pct_db_exception():
    from app.evals.integrations import get_uptime_pct
    with patch("app.db.postgres.execute_one", side_effect=Exception("db down")):
        pct = get_uptime_pct("gmail")
    assert pct is None
