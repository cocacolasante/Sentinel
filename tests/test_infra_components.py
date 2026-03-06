"""
Tests for:
- app/evals/runner.py (EvalRunner helpers)
- app/evals/scheduler.py (start/stop/get_scheduler)
- app/telos/loader.py (TelosLoader)
- app/integrations/milestone_logger.py (_get_label, _build_summary, log_milestone)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Stub out modules not available locally but present in CI
_MISSING = ["tenacity", "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.asyncio", "apscheduler.triggers", "apscheduler.triggers.cron"]
for _mod in _MISSING:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


# ── telos/loader.py ───────────────────────────────────────────────────────────


def test_telos_loader_missing_dir(tmp_path):
    from app.telos.loader import TelosLoader
    loader = TelosLoader(str(tmp_path / "nonexistent"))
    block = loader.get_block()
    assert block == ""


def test_telos_loader_empty_dir(tmp_path):
    from app.telos.loader import TelosLoader
    loader = TelosLoader(str(tmp_path))
    block = loader.get_block()
    assert block == ""


def test_telos_loader_single_file(tmp_path):
    from app.telos.loader import TelosLoader
    (tmp_path / "mission.md").write_text("Be helpful.")
    loader = TelosLoader(str(tmp_path))
    block = loader.get_block()
    assert "TELOS: MISSION" in block
    assert "Be helpful." in block
    assert "PERSONAL CONTEXT" in block


def test_telos_loader_multiple_files(tmp_path):
    from app.telos.loader import TelosLoader
    (tmp_path / "mission.md").write_text("Mission text")
    (tmp_path / "goals.md").write_text("Goals text")
    loader = TelosLoader(str(tmp_path))
    block = loader.get_block()
    assert "MISSION" in block
    assert "GOALS" in block


def test_telos_loader_cache_hit(tmp_path):
    from app.telos.loader import TelosLoader
    (tmp_path / "mission.md").write_text("Cached mission")
    loader = TelosLoader(str(tmp_path), cache_ttl_seconds=300)
    b1 = loader.get_block()
    # Modify file — should still get cached version
    (tmp_path / "mission.md").write_text("New content")
    b2 = loader.get_block()
    assert b1 == b2


def test_telos_loader_reload(tmp_path):
    from app.telos.loader import TelosLoader
    (tmp_path / "mission.md").write_text("Original")
    loader = TelosLoader(str(tmp_path))
    loader.get_block()
    (tmp_path / "mission.md").write_text("Updated")
    files = loader.reload()
    block = loader.get_block()
    assert "Updated" in block
    assert "mission.md" in files


def test_telos_loader_extras_alphabetical(tmp_path):
    from app.telos.loader import TelosLoader
    # Extra file not in _DEFAULT_ORDER
    (tmp_path / "zz_extra.md").write_text("extra content")
    loader = TelosLoader(str(tmp_path))
    block = loader.get_block()
    assert "ZZ_EXTRA" in block


def test_telos_loader_unreadable_file(tmp_path):
    from app.telos.loader import TelosLoader
    (tmp_path / "mission.md").write_text("Good")
    loader = TelosLoader(str(tmp_path))
    with patch("pathlib.Path.read_text", side_effect=IOError("permission denied")):
        block = loader.get_block()
    # Should not raise, may return empty or partial
    assert isinstance(block, str)


# ── integrations/milestone_logger.py ─────────────────────────────────────────


def test_get_label_known_action():
    from app.integrations.milestone_logger import _get_label
    emoji, label = _get_label("commit", "repo_commit")
    assert "📦" in emoji
    assert "Commit" in label


def test_get_label_ionos_cloud():
    from app.integrations.milestone_logger import _get_label
    emoji, label = _get_label("provision_server", "ionos_cloud")
    assert "☁️" in emoji
    assert "Cloud" in label


def test_get_label_ionos_dns():
    from app.integrations.milestone_logger import _get_label
    emoji, label = _get_label("create_record", "ionos_dns")
    assert "🌐" in emoji
    assert "DNS" in label


def test_get_label_n8n_manage():
    from app.integrations.milestone_logger import _get_label
    emoji, label = _get_label("create_workflow", "n8n_manage")
    assert "⚙️" in emoji


def test_get_label_sentry_prefix():
    from app.integrations.milestone_logger import _get_label
    emoji, label = _get_label("sentry_resolve_issue", "sentry_manage")
    assert "🐛" in emoji


def test_get_label_unknown():
    from app.integrations.milestone_logger import _get_label
    emoji, label = _get_label("unknown_action", "unknown_intent")
    assert "🤖" in emoji
    assert "AI Action" in label


def test_build_summary_with_path():
    from app.integrations.milestone_logger import _build_summary
    params = {"path": "app/main.py", "content": "..."}
    summary = _build_summary(params)
    assert "path" in summary
    assert "app/main.py" in summary


def test_build_summary_with_message():
    from app.integrations.milestone_logger import _build_summary
    params = {"message": "fix: resolve null pointer"}
    summary = _build_summary(params)
    assert "message" in summary
    assert "fix: resolve null pointer" in summary


def test_build_summary_empty_params():
    from app.integrations.milestone_logger import _build_summary
    assert _build_summary({}) == ""


def test_build_summary_multiple_keys():
    from app.integrations.milestone_logger import _build_summary
    params = {"path": "app/x.py", "message": "fix bug", "to": "user@example.com"}
    summary = _build_summary(params)
    # Should include up to 3 keys
    assert "path" in summary
    assert "message" in summary


async def test_log_milestone_db_and_slack():
    from app.integrations.milestone_logger import log_milestone
    with patch("app.db.postgres.execute") as mock_exec, \
         patch("app.integrations.slack_notifier.post_alert", new=AsyncMock()) as mock_alert:
        await log_milestone(
            action="commit",
            intent="repo_commit",
            params={"message": "fix: something", "files": ["app/main.py"]},
            session_id="test-session",
            agent="engineer",
        )
    mock_exec.assert_called_once()
    mock_alert.assert_called_once()


async def test_log_milestone_db_exception_no_raise():
    from app.integrations.milestone_logger import log_milestone
    with patch("app.db.postgres.execute", side_effect=Exception("db down")), \
         patch("app.integrations.slack_notifier.post_alert", new=AsyncMock()):
        # Should not raise
        await log_milestone("push", "repo_push", {}, "s1")


async def test_log_milestone_slack_exception_no_raise():
    from app.integrations.milestone_logger import log_milestone
    with patch("app.db.postgres.execute"), \
         patch("app.integrations.slack_notifier.post_alert", new=AsyncMock(side_effect=Exception("slack down"))):
        # Should not raise
        await log_milestone("push", "repo_push", {}, "s1")


async def test_log_milestone_with_detail():
    from app.integrations.milestone_logger import log_milestone
    with patch("app.db.postgres.execute") as mock_exec, \
         patch("app.integrations.slack_notifier.post_alert", new=AsyncMock()):
        await log_milestone(
            action="deploy_brain",
            intent="server_shell",
            params={"service": "brain"},
            session_id="s1",
            detail={"pr_url": "https://github.com/org/repo/pull/5"},
        )
    # detail dict used instead of params
    call_args = mock_exec.call_args[0]
    assert "pull/5" in call_args[1][4]  # detail JSON in 5th positional arg


# ── evals/runner.py helpers ───────────────────────────────────────────────────


def test_eval_runner_load_cases_empty_dir(tmp_path):
    from app.evals.runner import EvalRunner
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    cases = runner._load_cases(tmp_path, "engineer")
    assert cases == []


def test_eval_runner_load_cases_with_files(tmp_path):
    import json as _json
    from app.evals.runner import EvalRunner
    data = {"input": "hello", "criteria": ["responds"], "judge_prompt": "Score 0-10", "threshold": 7}
    (tmp_path / "test_hello.json").write_text(_json.dumps(data))
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    cases = runner._load_cases(tmp_path, "engineer")
    assert len(cases) == 1
    assert cases[0].name == "test_hello"


def test_eval_runner_load_cases_bad_json(tmp_path):
    from app.evals.runner import EvalRunner
    (tmp_path / "test_bad.json").write_text("not valid json {")
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    # Should not raise — just skip bad file
    cases = runner._load_cases(tmp_path, "engineer")
    assert cases == []


def test_eval_runner_persist_summaries_no_crash():
    from app.evals.runner import EvalRunner
    from app.evals.base import AgentEvalSummary
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    with patch("app.db.postgres.execute"):
        runner._persist_summaries([
            AgentEvalSummary("engineer", "r1", 8.0, 1.0, 1, 1, [])
        ])


def test_eval_runner_persist_summaries_db_exception():
    from app.evals.runner import EvalRunner
    from app.evals.base import AgentEvalSummary
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    with patch("app.db.postgres.execute", side_effect=Exception("db down")):
        runner._persist_summaries([
            AgentEvalSummary("engineer", "r1", 8.0, 1.0, 1, 1, [])
        ])  # Should not raise


def test_eval_runner_get_previous_avg_no_data():
    from app.evals.runner import EvalRunner
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    with patch("app.db.postgres.execute_one", return_value={"avg_score": None}):
        result = runner.get_previous_avg("engineer", "r1")
    assert result is None


def test_eval_runner_get_previous_avg_with_data():
    from app.evals.runner import EvalRunner
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    with patch("app.db.postgres.execute_one", return_value={"avg_score": 7.5}):
        result = runner.get_previous_avg("engineer", "r1")
    assert result == 7.5


def test_eval_runner_get_previous_avg_exception():
    from app.evals.runner import EvalRunner
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    with patch("app.db.postgres.execute_one", side_effect=Exception("db down")):
        result = runner.get_previous_avg("engineer", "r1")
    assert result is None


async def test_eval_runner_run_agent_empty_dir(tmp_path):
    from app.evals.runner import EvalRunner
    with patch("app.brain.llm_router.LLMRouter"), \
         patch("app.agents.registry.AgentRegistry"):
        runner = EvalRunner()
    runner._llm = MagicMock()
    runner._agents = MagicMock()
    summary = await runner.run_agent("engineer", tmp_path, run_id="test-run")
    assert summary.agent_name == "engineer"
    assert summary.total_tests == 0
    assert summary.avg_score == 0.0


# ── evals/scheduler.py ────────────────────────────────────────────────────────


def test_get_scheduler_returns_none_initially():
    import app.evals.scheduler as sched_mod
    # Reset state
    sched_mod._scheduler = None
    result = sched_mod.get_scheduler()
    assert result is None


def test_stop_scheduler_when_none():
    import app.evals.scheduler as sched_mod
    sched_mod._scheduler = None
    sched_mod.stop_scheduler()  # Should not raise


def test_stop_scheduler_when_running():
    import app.evals.scheduler as sched_mod
    mock_sched = MagicMock()
    mock_sched.running = True
    sched_mod._scheduler = mock_sched
    sched_mod.stop_scheduler()
    mock_sched.shutdown.assert_called_once_with(wait=False)
    assert sched_mod._scheduler is None


def test_start_scheduler():
    import app.evals.scheduler as sched_mod
    mock_sched = MagicMock()
    mock_job = MagicMock()
    mock_job.name = "Weekly Agent Quality Evals"
    mock_job.next_run_time = None
    mock_sched.get_jobs.return_value = [mock_job]
    with patch("app.evals.scheduler.AsyncIOScheduler", return_value=mock_sched):
        result = sched_mod.start_scheduler()
    assert result == mock_sched
    mock_sched.start.assert_called_once()
    assert mock_sched.add_job.call_count == 2
