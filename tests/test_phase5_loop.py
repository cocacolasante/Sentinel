"""
Phase 5 — Closing the Loop test suite.

Tests for AutonomyGradientSkill, ProposalExecutorSkill, PromptRefinementSkill,
SkillEvolutionSkill, ReflectionSkill.dispatch_proposals, WakeSkill Phase 5 additions,
GitCommitSkill.post_merge_hook, and SelfImprovementDashboardSkill.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _make_proposal(
    title="Fix slow skill",
    description="Improve latency",
    priority=5.0,
    auto_actionable=True,
    type="self_heal",
    skill_file=None,
    detail="Detailed fix description",
    estimated_impact="medium",
    skill=None,
):
    from app.skills.reflection_skill import ReflectionProposal
    return ReflectionProposal(
        title=title,
        description=description,
        priority=priority,
        auto_actionable=auto_actionable,
        type=type,
        skill_file=skill_file,
        detail=detail,
        estimated_impact=estimated_impact,
        skill=skill,
    )


# ---------------------------------------------------------------------------
# AutonomyGradientSkill
# ---------------------------------------------------------------------------

class TestAutonomyGradientSkill:

    @pytest.mark.asyncio
    async def test_high_score_auto_approves(self):
        """success_rate >= high_threshold → apply_gradient returns True."""
        from app.skills.autonomy_gradient_skill import AutonomyScore

        score = AutonomyScore(
            score=0.90,
            success_rate=0.95,
            avg_duration_ms=100.0,
            sample_size=50,
            recommendation="increase",
            reasoning="test",
        )

        with patch("app.skills.autonomy_gradient_skill._check_override_sync", return_value=False):
            result = score.apply_gradient("goal_execution")

        assert result is True

    def test_emergency_brake(self):
        """sentinel:autonomy_override set → apply_gradient always False."""
        from app.skills.autonomy_gradient_skill import AutonomyScore
        import app.skills.autonomy_gradient_skill as ag_mod

        score = AutonomyScore(
            score=0.99,
            success_rate=0.99,
            avg_duration_ms=10.0,
            sample_size=100,
            recommendation="increase",
            reasoning="test",
        )

        with patch("app.skills.autonomy_gradient_skill._check_override_sync", return_value=True):
            with patch("app.config.get_settings") as mock_settings:
                cfg = MagicMock()
                cfg.sentinel_autonomy_high_threshold = 0.85
                mock_settings.return_value = cfg
                result = score.apply_gradient("goal_execution")

        assert result is False

    @pytest.mark.asyncio
    async def test_insufficient_samples(self):
        """< min_sample_size → score=0.5, recommendation='maintain'."""
        from app.skills.autonomy_gradient_skill import AutonomyGradientSkill

        mock_observer = MagicMock()
        mock_observer.success_rate_by_skill = AsyncMock(return_value={"skill_a": 0.9})
        mock_observer.avg_tokens_by_skill = AsyncMock(return_value={"skill_a": 500})

        skill = AutonomyGradientSkill()

        with patch("app.skills.autonomy_gradient_skill.AutonomyGradientSkill._cache_score", new_callable=AsyncMock):
            with patch("app.skills.observer_skill.get_observer", return_value=mock_observer):
                with patch("app.config.get_settings") as mock_settings:
                    cfg = MagicMock()
                    cfg.sentinel_autonomy_min_sample_size = 20
                    cfg.sentinel_autonomy_high_threshold = 0.85
                    cfg.sentinel_autonomy_low_threshold = 0.50
                    mock_settings.return_value = cfg

                    result = await skill.compute(lookback_hours=24)

        assert result.score == 0.5
        assert result.recommendation == "maintain"
        assert result.sample_size == 1  # only 1 skill returned


# ---------------------------------------------------------------------------
# ProposalExecutorSkill
# ---------------------------------------------------------------------------

class TestProposalExecutorSkill:

    @pytest.mark.asyncio
    async def test_routes_prompt_change_to_prompt_refinement(self):
        """type='prompt_change' → goal enqueued with skill_hint='prompt_refinement'."""
        import app.skills.proposal_executor_skill as pe_mod
        from app.skills.proposal_executor_skill import ProposalExecutorSkill

        proposal = _make_proposal(type="prompt_change", priority=5.0)

        mock_queue = MagicMock()
        mock_queue.enqueue = AsyncMock()

        orig_gq = pe_mod.get_goal_queue
        orig_pg = pe_mod.pg_execute
        pe_mod.get_goal_queue = lambda: mock_queue
        pe_mod.pg_execute = AsyncMock()

        try:
            with patch("app.config.get_settings") as mock_settings:
                cfg = MagicMock()
                cfg.sentinel_goal_max_priority_auto = 7.0
                cfg.sentinel_skill_evolution_enabled = True
                mock_settings.return_value = cfg

                skill = ProposalExecutorSkill()
                dispatched = await skill.build_goal(proposal)
        finally:
            pe_mod.get_goal_queue = orig_gq
            pe_mod.pg_execute = orig_pg

        assert dispatched.skill_hint == "prompt_refinement"
        assert dispatched.goal_type == "prompt_change"
        mock_queue.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_caps_priority(self):
        """proposal.priority=9.0 → goal.priority == sentinel_goal_max_priority_auto."""
        import app.skills.proposal_executor_skill as pe_mod
        from app.skills.proposal_executor_skill import ProposalExecutorSkill

        proposal = _make_proposal(type="self_heal", priority=9.0)

        mock_queue = MagicMock()
        mock_queue.enqueue = AsyncMock()

        orig_gq = pe_mod.get_goal_queue
        orig_pg = pe_mod.pg_execute
        pe_mod.get_goal_queue = lambda: mock_queue
        pe_mod.pg_execute = AsyncMock()

        try:
            with patch("app.config.get_settings") as mock_settings:
                cfg = MagicMock()
                cfg.sentinel_goal_max_priority_auto = 7.0
                cfg.sentinel_skill_evolution_enabled = True
                mock_settings.return_value = cfg

                skill = ProposalExecutorSkill()
                dispatched = await skill.build_goal(proposal)
        finally:
            pe_mod.get_goal_queue = orig_gq
            pe_mod.pg_execute = orig_pg

        assert dispatched.priority <= 7.0

    @pytest.mark.asyncio
    async def test_skips_skill_evolution_when_disabled(self):
        """sentinel_skill_evolution_enabled=False → no goal enqueued, empty goal_id."""
        import app.skills.proposal_executor_skill as pe_mod
        from app.skills.proposal_executor_skill import ProposalExecutorSkill

        proposal = _make_proposal(type="new_skill", priority=5.0)

        mock_queue = MagicMock()
        mock_queue.enqueue = AsyncMock()

        orig_gq = pe_mod.get_goal_queue
        pe_mod.get_goal_queue = lambda: mock_queue

        try:
            with patch("app.config.get_settings") as mock_settings:
                cfg = MagicMock()
                cfg.sentinel_goal_max_priority_auto = 7.0
                cfg.sentinel_skill_evolution_enabled = False
                mock_settings.return_value = cfg

                skill = ProposalExecutorSkill()
                dispatched = await skill.build_goal(proposal)
        finally:
            pe_mod.get_goal_queue = orig_gq

        mock_queue.enqueue.assert_not_called()
        assert dispatched.goal_id == ""


# ---------------------------------------------------------------------------
# PromptRefinementSkill
# ---------------------------------------------------------------------------

class TestPromptRefinementSkill:

    @pytest.mark.asyncio
    async def test_propose_variant_writes_to_db(self):
        """mock Sonnet → variant written to DB with variant='treatment'."""
        from app.skills.prompt_refinement_skill import PromptRefinementSkill

        skill = PromptRefinementSkill()

        mock_content = MagicMock()
        mock_content.text = "Improved prompt text for testing."

        mock_resp = MagicMock()
        mock_resp.content = [mock_content]

        upserted_variants = []

        async def mock_upsert(skill_name, variant, prompt_hash):
            upserted_variants.append((skill_name, variant, prompt_hash))

        with patch("anthropic.Anthropic") as MockAnthropic:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_resp
            MockAnthropic.return_value = mock_client

            with patch.object(skill, "_upsert_variant", side_effect=mock_upsert):
                with patch("app.config.get_settings") as mock_settings:
                    cfg = MagicMock()
                    cfg.anthropic_api_key = "test-key"
                    cfg.model_sonnet = "claude-sonnet-4-6"
                    mock_settings.return_value = cfg

                    variant = await skill.propose_variant(
                        "test_skill",
                        "Original prompt",
                        "Make it better",
                    )

        assert variant.variant == "treatment"
        assert variant.skill_name == "test_skill"
        # Both control and treatment should be upserted
        variant_names = [v[1] for v in upserted_variants]
        assert "control" in variant_names
        assert "treatment" in variant_names

    @pytest.mark.asyncio
    async def test_evaluate_inconclusive_insufficient_samples(self):
        """< min_samples → winner='inconclusive'."""
        import app.skills.prompt_refinement_skill as pr_mod
        from app.skills.prompt_refinement_skill import PromptRefinementSkill

        skill = PromptRefinementSkill()

        mock_rows = [
            {"variant": "control", "calls_total": 10, "calls_success": 8},
            {"variant": "treatment", "calls_total": 10, "calls_success": 9},
        ]

        orig_pg = pr_mod.pg_execute
        pr_mod.pg_execute = AsyncMock(return_value=mock_rows)

        try:
            with patch("app.config.get_settings") as mock_settings:
                cfg = MagicMock()
                cfg.sentinel_prompt_ab_min_samples = 50
                cfg.sentinel_prompt_ab_confidence = 0.95
                mock_settings.return_value = cfg

                result = await skill.evaluate("test_skill")
        finally:
            pr_mod.pg_execute = orig_pg

        assert result.winner == "inconclusive"

    @pytest.mark.asyncio
    async def test_apply_winner_calls_git_commit(self):
        """Confident treatment winner → GitCommitSkill.execute called."""
        import app.skills.prompt_refinement_skill as pr_mod
        from app.skills.prompt_refinement_skill import PromptRefinementSkill, ABTestResult

        skill = PromptRefinementSkill()

        confident_result = ABTestResult(
            skill_name="test_skill",
            winner="treatment",
            confidence=0.97,
            treatment_success_rate=0.90,
            control_success_rate=0.70,
            recommendation="treatment wins",
        )

        mock_gc = MagicMock()
        mock_gc.execute = AsyncMock(return_value=MagicMock(context_data="PR opened: https://github.com/test/pull/1", is_error=False))

        orig_gc = pr_mod.GitCommitSkill
        orig_pg = pr_mod.pg_execute
        pr_mod.GitCommitSkill = lambda: mock_gc
        pr_mod.pg_execute = AsyncMock()

        try:
            with patch.object(skill, "evaluate", new_callable=AsyncMock, return_value=confident_result):
                with patch("app.config.get_settings") as mock_settings:
                    cfg = MagicMock()
                    cfg.sentinel_prompt_ab_min_samples = 50
                    cfg.sentinel_prompt_ab_confidence = 0.95
                    mock_settings.return_value = cfg

                    with patch("app.integrations.slack_notifier.post_alert_sync"):
                        await skill.apply_winner("test_skill")
        finally:
            pr_mod.GitCommitSkill = orig_gc
            pr_mod.pg_execute = orig_pg

        mock_gc.execute.assert_called_once()


# ---------------------------------------------------------------------------
# SkillEvolutionSkill
# ---------------------------------------------------------------------------

class TestSkillEvolutionSkill:

    @pytest.mark.asyncio
    async def test_disabled_guard(self):
        """sentinel_skill_evolution_enabled=False → SkillResult error, no file created."""
        from app.skills.skill_evolution_skill import SkillEvolutionSkill

        skill = SkillEvolutionSkill()

        with patch("app.config.get_settings") as mock_settings:
            cfg = MagicMock()
            cfg.sentinel_skill_evolution_enabled = False
            mock_settings.return_value = cfg

            result = await skill.execute({"title": "make a new skill"}, "")

        assert result.is_error is True
        assert "disabled" in result.context_data

    @pytest.mark.asyncio
    async def test_sandbox_validates_before_commit(self):
        """mock SandboxValidatorSkill pass → GitCommitSkill called."""
        from app.skills.skill_evolution_skill import SkillEvolutionSkill

        skill = SkillEvolutionSkill()

        generated_code = '''"""Test skill."""
from app.skills.base import BaseSkill, SkillResult

class TestEvolutionSkill(BaseSkill):
    name = "test_evolution"
    description = "Test evolved skill"
    trigger_intents = ["test_evolve"]

    async def execute(self, params: dict, original_message: str = "") -> SkillResult:
        return SkillResult(context_data="ok")
'''

        with patch("app.config.get_settings") as mock_settings:
            cfg = MagicMock()
            cfg.sentinel_skill_evolution_enabled = True
            cfg.anthropic_api_key = "test-key"
            cfg.model_sonnet = "claude-sonnet-4-6"
            cfg.model_haiku = "claude-haiku-4-5-20251001"
            mock_settings.return_value = cfg

            with patch.object(skill, "_search_similar_skills", new_callable=AsyncMock, return_value=""):
                with patch.object(skill, "_generate_skill", new_callable=AsyncMock, return_value=(generated_code, "test_evolution", ["test_evolve"])):
                    with patch.object(skill, "_validate", new_callable=AsyncMock, return_value=True):
                        with patch.object(skill, "_generate_tests", new_callable=AsyncMock, return_value="def test_placeholder(): pass"):
                            with patch.object(skill, "_commit_pr", new_callable=AsyncMock, return_value="https://github.com/test/pull/42"):
                                with patch("app.integrations.slack_notifier.post_alert_sync"):
                                    from app.skills.reflection_skill import ReflectionProposal
                                    proposal = ReflectionProposal(
                                        title="Test skill",
                                        description="Test",
                                        priority=5.0,
                                        auto_actionable=True,
                                        type="new_skill",
                                        detail="Write a test skill",
                                    )
                                    evolved = await skill.evolve(proposal)

        assert evolved.skill_name == "test_evolution"
        assert evolved.pr_url == "https://github.com/test/pull/42"


# ---------------------------------------------------------------------------
# ReflectionSkill
# ---------------------------------------------------------------------------

class TestReflectionSkillPhase5:

    @pytest.mark.asyncio
    async def test_dispatch_proposals_called_for_auto_actionable(self):
        """reflect() with auto_actionable proposals → dispatch_proposals() invoked."""
        from app.skills.reflection_skill import ReflectionSkill, ReflectionProposal

        skill = ReflectionSkill()

        proposals = [
            _make_proposal(title="Fix A", auto_actionable=True),
            _make_proposal(title="Fix B", auto_actionable=False),
        ]

        dispatched = []

        async def mock_dispatch(proposal_list):
            dispatched.extend(proposal_list)

        with patch.object(skill, "dispatch_proposals", side_effect=mock_dispatch):
            with patch.object(skill, "_call_sonnet", new_callable=AsyncMock, return_value={
                "observation_count": 5,
                "observations": ["test"],
                "proposals": [
                    {"title": "Fix A", "description": "desc", "priority": 5.0, "auto_actionable": True, "type": "self_heal"},
                    {"title": "Fix B", "description": "desc", "priority": 5.0, "auto_actionable": False, "type": "self_heal"},
                ],
            }):
                with patch.object(skill, "_persist", new_callable=AsyncMock):
                    with patch("app.skills.reflection_skill.get_observer") as mock_get_obs:
                        mock_obs = MagicMock()
                        mock_obs.failures_last_n_hours = AsyncMock(return_value=[{"error": "test"}])
                        mock_obs.success_rate_by_skill = AsyncMock(return_value={"skill_a": 0.8})
                        mock_obs.avg_tokens_by_skill = AsyncMock(return_value={})
                        mock_obs.most_common_errors = AsyncMock(return_value=[])
                        mock_get_obs.return_value = mock_obs

                        with patch("app.config.get_settings") as mock_settings:
                            cfg = MagicMock()
                            cfg.sentinel_reflection_lookback_hours = 24
                            cfg.sentinel_goal_max_priority_auto = 7.0
                            cfg.model_sonnet = "claude-sonnet-4-6"
                            cfg.anthropic_api_key = "test-key"
                            mock_settings.return_value = cfg

                            with patch.object(skill, "_get_previous_proposals", new_callable=AsyncMock, return_value=[]):
                                with patch("app.integrations.slack_notifier.post_alert_sync"):
                                    await skill.reflect(lookback_hours=24)

        # dispatch_proposals was called with all proposals
        assert len(dispatched) == 2


# ---------------------------------------------------------------------------
# WakeSkill Phase 5
# ---------------------------------------------------------------------------

class TestWakeSkillPhase5:

    @pytest.mark.asyncio
    async def test_consults_autonomy_gradient_low_score(self):
        """Mock score below threshold → goal not auto-executed (status set to needs_approval)."""
        from app.skills.wake_skill import WakeSkill
        from app.skills.autonomy_gradient_skill import AutonomyScore

        skill = WakeSkill()

        low_score = AutonomyScore(
            score=0.40,
            success_rate=0.40,
            avg_duration_ms=500.0,
            sample_size=30,
            recommendation="decrease",
            reasoning="low score test",
        )

        mock_goal = MagicMock()
        mock_goal.id = "test-goal-123"
        mock_goal.title = "Test goal"
        mock_goal.description = "desc"

        mock_queue = MagicMock()
        mock_queue.update_status = AsyncMock()

        import app.skills.wake_skill as ws_mod
        import app.skills.autonomy_gradient_skill as ag_mod

        orig_ag = ws_mod.AutonomyGradientSkill
        mock_ag_instance = MagicMock()
        mock_ag_instance.get_current = AsyncMock(return_value=low_score)
        ws_mod.AutonomyGradientSkill = lambda: mock_ag_instance

        orig_override = ag_mod._check_override_sync
        ag_mod._check_override_sync = lambda: False

        try:
            with patch("app.config.get_settings") as mock_settings:
                cfg = MagicMock()
                cfg.brain_autonomy = True
                cfg.sentinel_autonomy_high_threshold = 0.85
                mock_settings.return_value = cfg

                with patch("app.skills.goal_queue_skill.get_goal_queue", return_value=mock_queue):
                    await skill._handle_execute_goal(mock_goal)
        finally:
            ws_mod.AutonomyGradientSkill = orig_ag
            ag_mod._check_override_sync = orig_override

        # Should have marked goal as needs_approval, not executed it
        mock_queue.update_status.assert_called_once_with("test-goal-123", "needs_approval")

    @pytest.mark.asyncio
    async def test_pr_merge_triggers_post_merge_hook(self):
        """Merged PR with sentinel-autofix label → post_merge_hook called."""
        from app.skills.wake_skill import WakeSkill

        skill = WakeSkill()

        mock_gh_result = MagicMock()
        mock_gh_result.is_error = False
        mock_gh_result.context_data = json.dumps([
            {"number": 42, "merged": True, "head": {"ref": "sentinel/evolved-skill-myskill"}, "metadata": {"evolved_skill": "myskill"}},
        ])

        mock_gc = MagicMock()
        mock_gc.post_merge_hook = AsyncMock()

        import app.skills.wake_skill as ws_mod

        orig_gh = ws_mod.GitHubReadSkill
        orig_gc = ws_mod.GitCommitSkill

        mock_gh_instance = MagicMock()
        mock_gh_instance.execute = AsyncMock(return_value=mock_gh_result)
        ws_mod.GitHubReadSkill = lambda: mock_gh_instance
        ws_mod.GitCommitSkill = lambda: mock_gc

        try:
            await skill._check_merged_prs()
        finally:
            ws_mod.GitHubReadSkill = orig_gh
            ws_mod.GitCommitSkill = orig_gc

        mock_gc.post_merge_hook.assert_called_once_with(42, "myskill")


# ---------------------------------------------------------------------------
# GitCommitSkill.post_merge_hook
# ---------------------------------------------------------------------------

class TestGitCommitPostMergeHook:

    @pytest.mark.asyncio
    async def test_reloads_module_and_evicts_from_sys_modules(self):
        """post_merge_hook with skill_name → sys.modules evicted + re-imported."""
        import importlib
        from app.skills.git_commit_skill import GitCommitSkill

        skill = GitCommitSkill()

        # Fake module in sys.modules
        fake_mod = MagicMock()
        sys.modules["app.skills.myskill"] = fake_mod

        imported = []
        orig_import = importlib.import_module

        def _tracking_import(name, *args, **kwargs):
            imported.append(name)
            if name == "app.skills.myskill":
                return MagicMock()
            return orig_import(name, *args, **kwargs)

        with patch("app.skills.git_commit_skill.importlib.import_module", side_effect=_tracking_import):
            with patch("app.integrations.slack_notifier.post_alert_sync"):
                with patch("asyncio.to_thread", new_callable=AsyncMock):
                    await skill.post_merge_hook(42, "myskill")

        # Module should have been evicted
        assert "app.skills.myskill" not in sys.modules
        # And re-imported
        assert "app.skills.myskill" in imported


# ---------------------------------------------------------------------------
# SelfImprovementDashboardSkill
# ---------------------------------------------------------------------------

class TestSelfImprovementDashboard:

    @pytest.mark.asyncio
    async def test_generates_report_with_mocked_data(self):
        """Mock all DB/Redis → SelfImprovementReport non-null with expected fields."""
        from app.skills.self_improvement_dashboard_skill import SelfImprovementDashboardSkill

        skill = SelfImprovementDashboardSkill()

        with patch.object(skill, "_query_goals", new_callable=AsyncMock, return_value=(10, 2, ["slow_skill"])):
            with patch.object(skill, "_query_proposals", new_callable=AsyncMock, return_value=(5, {"self_heal": 3, "prompt_change": 2})):
                with patch.object(skill, "_query_ab_winners", new_callable=AsyncMock, return_value=["skill_x"]):
                    with patch.object(skill, "_read_autonomy_trend", new_callable=AsyncMock, return_value=(0.78, "improving")):
                        with patch.object(skill, "_count_evolved_skills", new_callable=AsyncMock, return_value=1):
                            with patch.object(skill, "_generate_summary", new_callable=AsyncMock, return_value="Good progress this period."):
                                report = await skill.generate(24)

        assert report.goals_completed == 10
        assert report.goals_failed == 2
        assert report.proposals_dispatched == 5
        assert report.autonomy_score == 0.78
        assert report.autonomy_trend == "improving"
        assert report.skills_evolved == 1
        assert "skill_x" in report.prompt_ab_winners
        assert report.reflection_summary != ""

    @pytest.mark.asyncio
    async def test_empty_db_graceful(self):
        """Empty DB → report with zeros, no LLM call, no crash."""
        from app.skills.self_improvement_dashboard_skill import SelfImprovementDashboardSkill

        skill = SelfImprovementDashboardSkill()

        with patch.object(skill, "_query_goals", new_callable=AsyncMock, return_value=(0, 0, [])):
            with patch.object(skill, "_query_proposals", new_callable=AsyncMock, return_value=(0, {})):
                with patch.object(skill, "_query_ab_winners", new_callable=AsyncMock, return_value=[]):
                    with patch.object(skill, "_read_autonomy_trend", new_callable=AsyncMock, return_value=(0.5, "stable")):
                        with patch.object(skill, "_count_evolved_skills", new_callable=AsyncMock, return_value=0):
                            with patch.object(skill, "_generate_summary", new_callable=AsyncMock) as mock_summary:
                                report = await skill.generate(24)

        assert report.goals_completed == 0
        assert report.goals_failed == 0
        assert report.proposals_dispatched == 0
        # No LLM call when no data
        mock_summary.assert_not_called()
        assert report.reflection_summary == ""
