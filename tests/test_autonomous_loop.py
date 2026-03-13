"""
Tests for Phase 4 — Autonomous Agent Loop skills.

All external I/O (postgres, redis, Anthropic API) is mocked.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── GoalQueueSkill ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_goal_queue_enqueue_dequeue_ordering():
    """Dequeue returns highest-priority goal first."""
    from app.skills.goal_queue_skill import GoalQueueSkill, Goal

    stored: dict[str, bytes] = {}
    scores: dict[str, float] = {}

    class FakePipeline:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def execute(self):
            pass
        def zadd(self, key, mapping):
            scores.update(mapping)
        def setex(self, key, ttl, val):
            stored[key] = val.encode() if isinstance(val, str) else val

    mock_redis = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=FakePipeline())
    mock_redis.zcard = AsyncMock(return_value=3)

    # zrevrange returns IDs sorted by score descending
    async def fake_zrevrange(key, start, end):
        sorted_ids = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
        return [s.encode() for s in sorted_ids[start:end + 1]]

    async def fake_get(key):
        return stored.get(key)

    mock_redis.zrevrange = fake_zrevrange
    mock_redis.get = fake_get
    mock_redis.zrem = AsyncMock()

    goals = [
        Goal(id="low",  title="Low priority",  description="", created_by="user:a",
             created_at=datetime.now(timezone.utc), priority=2.0, status="pending"),
        Goal(id="high", title="High priority", description="", created_by="user:a",
             created_at=datetime.now(timezone.utc), priority=9.0, status="pending"),
        Goal(id="mid",  title="Mid priority",  description="", created_by="user:a",
             created_at=datetime.now(timezone.utc), priority=5.0, status="pending"),
    ]

    with patch("app.db.redis.get_redis", new_callable=AsyncMock, return_value=mock_redis):
        with patch("app.observability.prometheus_metrics.GOAL_QUEUE_DEPTH") as mock_depth:
            mock_depth.set = MagicMock()
            skill = GoalQueueSkill()
            for g in goals:
                await skill.enqueue(g)

            peeked = await skill.peek(3)

    # Should be ordered high → mid → low
    assert peeked[0].id == "high"
    assert peeked[1].id == "mid"
    assert peeked[2].id == "low"


@pytest.mark.asyncio
async def test_goal_queue_auto_goal_priority_cap():
    """Goals created by a skill (not user) cannot exceed sentinel_goal_max_priority_auto."""
    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.sentinel_goal_max_priority_auto = 7.0
        mock_settings.return_value = s

        enqueued_goals = []

        class FakePipeline:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def execute(self): pass
            def zadd(self, key, mapping): pass
            def setex(self, key, ttl, val):
                data = json.loads(val)
                enqueued_goals.append(data)

        mock_redis = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=FakePipeline())
        mock_redis.zcard = AsyncMock(return_value=1)

        with patch("app.db.redis.get_redis", new_callable=AsyncMock, return_value=mock_redis):
            with patch("app.observability.prometheus_metrics.GOAL_QUEUE_DEPTH") as mock_depth:
                mock_depth.set = MagicMock()
                from app.skills.goal_queue_skill import GoalQueueSkill
                skill = GoalQueueSkill()
                result = await skill.execute(
                    {"title": "Auto goal", "priority": 9.5, "created_by": "skill:wake"},
                    original_message="",
                )

    assert enqueued_goals, "Should have enqueued a goal"
    assert enqueued_goals[0]["priority"] <= 7.0


# ── ObserverSkill ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_observer_records_execution_event():
    """record() inserts a row into sentinel_execution_log."""
    from app.skills.observer_skill import ObserverSkill, ExecutionEvent

    inserted_query = []
    inserted_args = []

    async def mock_execute(query, *args):
        inserted_query.append(query)
        inserted_args.extend(args)

    with patch("app.db.postgres.execute", side_effect=mock_execute):
        with patch("app.observability.prometheus_metrics.SKILL_EXECUTIONS_TOTAL") as mock_counter:
            mock_counter.labels.return_value.inc = MagicMock()
            with patch("app.observability.prometheus_metrics.SKILL_DURATION_MS") as mock_hist:
                mock_hist.labels.return_value.observe = MagicMock()

                skill = ObserverSkill()
                event = ExecutionEvent(
                    skill_name="docker_drift",
                    status="success",
                    goal_id="goal-123",
                    duration_ms=250,
                    parameters={"server": "web-01"},
                )
                await skill.record(event)

    assert inserted_query, "INSERT should have been called"
    assert "sentinel_execution_log" in inserted_query[0]
    assert "docker_drift" in inserted_args


@pytest.mark.asyncio
async def test_observer_generates_summary_via_haiku():
    """generate_summary() calls Haiku and caches the result in Redis."""
    mock_content = MagicMock()
    mock_content.text = "docker_drift completed successfully on web-01"
    mock_response = MagicMock()
    mock_response.content = [mock_content]

    cached_values: dict = {}

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # Cache miss
    mock_redis.setex = AsyncMock(side_effect=lambda k, t, v: cached_values.update({k: v}))

    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.anthropic_api_key = "test-key"
        s.model_haiku = "claude-haiku-4-5-20251001"
        mock_settings.return_value = s

        with patch("anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_anthropic.return_value = mock_client

            with patch("app.db.redis.get_redis", new_callable=AsyncMock, return_value=mock_redis):
                from app.skills.observer_skill import ObserverSkill
                skill = ObserverSkill()
                summary = await skill.generate_summary(
                    status="success",
                    skill_name="docker_drift",
                    parameters={"server": "web-01"},
                )

    assert "docker_drift" in summary.lower() or "success" in summary.lower()
    assert cached_values, "Summary should be cached in Redis"


# ── PlannerSkill ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_planner_produces_valid_dag():
    """PlannerSkill produces a valid plan with no circular dependencies."""
    valid_plan = {
        "goal_id": "test-goal",
        "steps": [
            {
                "step_id": "step_1", "skill": "docker_drift", "description": "Check drift",
                "depends_on": [], "model_tier": "haiku", "estimated_input_tokens": 500,
                "estimated_output_tokens": 200, "timeout_seconds": 30,
                "on_failure": "abort", "parameters": {"server": "web-01"},
            },
            {
                "step_id": "step_2", "skill": "cert_check", "description": "Check certs",
                "depends_on": ["step_1"], "model_tier": "haiku", "estimated_input_tokens": 300,
                "estimated_output_tokens": 100, "timeout_seconds": 30,
                "on_failure": "skip", "parameters": {},
            },
        ],
        "estimated_total_tokens": 5000,
        "estimated_cost_usd": 0.01,
        "confidence": 0.9,
    }

    mock_content = MagicMock()
    mock_content.text = json.dumps(valid_plan)
    mock_response = MagicMock()
    mock_response.content = [mock_content]

    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.anthropic_api_key = "test"
        s.model_opus = "claude-opus-4-6"
        s.sentinel_plan_token_budget = 50000
        mock_settings.return_value = s

        with patch("anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_anthropic.return_value = mock_client

            with patch("app.db.postgres.execute", new_callable=AsyncMock):
                from app.skills.planner_skill import PlannerSkill
                skill = PlannerSkill()
                plan = await skill.plan(
                    goal_id="test-goal",
                    goal_title="Check infrastructure",
                    goal_description="Check docker drift and certs",
                    available_skills=["docker_drift", "cert_check", "chat"],
                )

    assert plan.goal_id == "test-goal"
    assert len(plan.steps) == 2
    assert plan.steps[0].step_id == "step_1"


@pytest.mark.asyncio
async def test_planner_retries_on_validation_failure():
    """PlannerSkill retries when a step references an invalid skill name."""
    invalid_plan = {
        "goal_id": "test-goal",
        "steps": [
            {"step_id": "s1", "skill": "nonexistent_skill", "description": "x",
             "depends_on": [], "model_tier": "haiku", "estimated_input_tokens": 100,
             "estimated_output_tokens": 50, "timeout_seconds": 30,
             "on_failure": "abort", "parameters": {}},
        ],
        "estimated_total_tokens": 100, "estimated_cost_usd": 0.001, "confidence": 0.5,
    }
    valid_plan = {
        "goal_id": "test-goal",
        "steps": [
            {"step_id": "s1", "skill": "chat", "description": "x",
             "depends_on": [], "model_tier": "haiku", "estimated_input_tokens": 100,
             "estimated_output_tokens": 50, "timeout_seconds": 30,
             "on_failure": "abort", "parameters": {}},
        ],
        "estimated_total_tokens": 100, "estimated_cost_usd": 0.001, "confidence": 0.9,
    }

    call_count = [0]

    def make_response(plan_dict):
        mock_content = MagicMock()
        mock_content.text = json.dumps(plan_dict)
        mock_resp = MagicMock()
        mock_resp.content = [mock_content]
        return mock_resp

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return make_response(invalid_plan)
        return make_response(valid_plan)

    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.anthropic_api_key = "test"
        s.model_opus = "claude-opus-4-6"
        s.sentinel_plan_token_budget = 50000
        mock_settings.return_value = s

        with patch("anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = side_effect
            mock_anthropic.return_value = mock_client

            with patch("app.db.postgres.execute", new_callable=AsyncMock):
                from app.skills.planner_skill import PlannerSkill
                skill = PlannerSkill()
                plan = await skill.plan(
                    goal_id="test-goal",
                    goal_title="Test",
                    goal_description="Test retry",
                    available_skills=["chat"],
                )

    assert call_count[0] == 2, "Should have retried once"
    assert plan.steps[0].skill == "chat"


# ── ExecutorSkill ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_executor_dry_run_calls_describe_not_execute():
    """In dry_run mode, skill.execute() should NOT be called."""
    from app.skills.planner_skill import ExecutionPlan, PlanStep
    from app.skills.executor_skill import ExecutorSkill

    execute_called = []
    mock_skill = MagicMock()
    mock_skill.execute = AsyncMock(side_effect=lambda *a, **kw: execute_called.append(True))

    mock_registry = MagicMock()
    mock_registry._skills = {"chat": mock_skill}

    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.brain_autonomy = False
        s.sentinel_max_concurrent_steps = 3
        mock_settings.return_value = s

        with patch("app.brain.dispatcher._build_skill_registry", return_value=mock_registry):
            with patch("app.skills.observer_skill.get_observer") as mock_obs:
                mock_observer = AsyncMock()
                mock_observer.record = AsyncMock()
                mock_observer.generate_summary = AsyncMock(return_value="dry run summary")
                mock_obs.return_value = mock_observer

                with patch("app.integrations.slack_notifier.post_alert_sync"):
                    plan = ExecutionPlan(
                        goal_id="test",
                        steps=[PlanStep(step_id="s1", skill="chat", description="test",
                                        depends_on=[], parameters={})],
                    )
                    skill = ExecutorSkill()
                    result = await skill.execute_plan(plan, dry_run=True)

    assert result["dry_run"] is True
    assert not execute_called, "skill.execute() must not be called in dry_run mode"


@pytest.mark.asyncio
async def test_executor_respects_abort_on_failure():
    """When step 1 fails with on_failure=abort, step 2 must not execute."""
    from app.skills.planner_skill import ExecutionPlan, PlanStep
    from app.skills.executor_skill import ExecutorSkill
    from app.skills.base import SkillResult

    executed_steps = []

    async def mock_skill_exec(params, original_message=""):
        executed_steps.append(params.get("_step", "unknown"))
        return SkillResult(context_data="error", is_error=True)

    mock_skill1 = MagicMock()
    mock_skill1.execute = AsyncMock(side_effect=lambda p, **kw: mock_skill_exec({**p, "_step": "step1"}))
    mock_skill2 = MagicMock()
    mock_skill2.execute = AsyncMock(side_effect=lambda p, **kw: mock_skill_exec({**p, "_step": "step2"}))

    mock_registry = MagicMock()
    mock_registry._skills = {"skill1": mock_skill1, "skill2": mock_skill2}

    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.brain_autonomy = True
        s.sentinel_max_concurrent_steps = 3
        mock_settings.return_value = s

        with patch("app.brain.dispatcher._build_skill_registry", return_value=mock_registry):
            with patch("app.skills.observer_skill.get_observer") as mock_obs:
                mock_observer = AsyncMock()
                mock_observer.record = AsyncMock()
                mock_observer.generate_summary = AsyncMock(return_value="fail summary")
                mock_obs.return_value = mock_observer

                with patch("app.integrations.slack_notifier.post_alert_sync"):
                    plan = ExecutionPlan(
                        goal_id="test",
                        steps=[
                            PlanStep(step_id="s1", skill="skill1", description="step1",
                                     depends_on=[], on_failure="abort", parameters={}),
                            PlanStep(step_id="s2", skill="skill2", description="step2",
                                     depends_on=[], on_failure="abort", parameters={}),
                        ],
                    )
                    executor = ExecutorSkill()
                    result = await executor.execute_plan(plan, dry_run=False)

    assert result["overall_status"] == "aborted"
    assert "step2" not in executed_steps


@pytest.mark.asyncio
async def test_executor_skips_autonomous_execution_without_brain_autonomy():
    """When brain_autonomy=False, executor forces dry_run=True."""
    from app.skills.planner_skill import ExecutionPlan, PlanStep
    from app.skills.executor_skill import ExecutorSkill
    from app.skills.base import SkillResult

    execute_calls = []

    mock_skill = MagicMock()
    mock_skill.execute = AsyncMock(side_effect=lambda *a, **kw: execute_calls.append(True) or SkillResult(context_data="ok"))

    mock_registry = MagicMock()
    mock_registry._skills = {"chat": mock_skill}

    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.brain_autonomy = False  # Autonomy OFF
        s.sentinel_max_concurrent_steps = 3
        mock_settings.return_value = s

        with patch("app.brain.dispatcher._build_skill_registry", return_value=mock_registry):
            with patch("app.skills.observer_skill.get_observer") as mock_obs:
                mock_observer = AsyncMock()
                mock_observer.record = AsyncMock()
                mock_observer.generate_summary = AsyncMock(return_value="summary")
                mock_obs.return_value = mock_observer

                with patch("app.integrations.slack_notifier.post_alert_sync"):
                    plan = ExecutionPlan(
                        goal_id="g1",
                        steps=[PlanStep(step_id="s1", skill="chat", description="test", depends_on=[], parameters={})],
                    )
                    executor = ExecutorSkill()
                    # Pass dry_run=False but autonomy is off — should force dry_run=True
                    result = await executor.execute_plan(plan, dry_run=False)

    assert result["dry_run"] is True
    assert not execute_calls, "No real execution when brain_autonomy=False"


# ── ReflectionSkill ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reflection_returns_no_data_report_gracefully():
    """When execution_log is empty, ReflectionSkill returns no-data report without LLM call."""
    with patch("app.skills.observer_skill.get_observer") as mock_obs_factory:
        mock_obs = AsyncMock()
        mock_obs.failures_last_n_hours = AsyncMock(return_value=[])
        mock_obs.success_rate_by_skill = AsyncMock(return_value={})
        mock_obs.avg_tokens_by_skill = AsyncMock(return_value={})
        mock_obs.most_common_errors = AsyncMock(return_value=[])
        mock_obs_factory.return_value = mock_obs

        with patch("anthropic.Anthropic") as mock_anthropic:
            with patch("app.db.postgres.execute", new_callable=AsyncMock):
                with patch("app.config.get_settings") as mock_settings:
                    s = MagicMock()
                    s.sentinel_reflection_lookback_hours = 24
                    s.anthropic_api_key = "test"
                    s.model_sonnet = "claude-sonnet-4-6"
                    s.sentinel_goal_max_priority_auto = 7.0
                    mock_settings.return_value = s

                    from app.skills.reflection_skill import ReflectionSkill
                    skill = ReflectionSkill()
                    report = await skill.reflect(lookback_hours=24)

        # LLM should NOT have been called
        mock_anthropic.assert_not_called()

    assert report["observation_count"] == 0
    assert report["proposals"] == []
    assert "No execution data" in report["message"]


# ── WakeSkill ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_sleep_path_completes_under_5s():
    """Empty queue, no alerts, no scheduled tasks → sleep in < 5s."""
    with patch("app.skills.goal_queue_skill.get_goal_queue") as mock_queue_factory:
        mock_queue = AsyncMock()
        mock_queue.peek = AsyncMock(return_value=[])
        mock_queue_factory.return_value = mock_queue

        with patch("app.db.postgres.execute", new_callable=AsyncMock, return_value=[]):
            with patch("app.observability.prometheus_metrics.WAKE_DECISIONS_TOTAL") as mock_counter:
                mock_counter.labels.return_value.inc = MagicMock()
                with patch("app.observability.prometheus_metrics.GOAL_QUEUE_DEPTH") as mock_depth:
                    mock_depth.set = MagicMock()

                    # Mock UTC time to something with no scheduled tasks (e.g., 10:30)
                    mock_now = datetime(2026, 3, 13, 10, 30, 0, tzinfo=timezone.utc)
                    with patch("app.skills.wake_skill.datetime") as mock_dt:
                        mock_dt.now.return_value = mock_now
                        mock_dt.fromisoformat = datetime.fromisoformat

                        from app.skills.wake_skill import WakeSkill
                        skill = WakeSkill()
                        start = time.monotonic()
                        decision = await skill.wake()
                        elapsed = time.monotonic() - start

    assert decision.action == "sleep"
    assert elapsed < 5.0, f"Sleep path took {elapsed:.2f}s (must be < 5s)"


@pytest.mark.asyncio
async def test_wake_enqueues_cert_check_at_0200():
    """At 02:00 UTC, WakeSkill should enqueue a cert_check scheduled goal."""
    enqueued_goals = []

    with patch("app.skills.goal_queue_skill.get_goal_queue") as mock_queue_factory:
        mock_queue = AsyncMock()
        mock_queue.peek = AsyncMock(return_value=[])
        mock_queue.enqueue = AsyncMock(side_effect=lambda g: enqueued_goals.append(g))
        mock_queue_factory.return_value = mock_queue

        with patch("app.db.postgres.execute", new_callable=AsyncMock, return_value=[]):
            with patch("app.observability.prometheus_metrics.WAKE_DECISIONS_TOTAL") as mock_counter:
                mock_counter.labels.return_value.inc = MagicMock()
                with patch("app.observability.prometheus_metrics.GOAL_QUEUE_DEPTH") as mock_depth:
                    mock_depth.set = MagicMock()

                    # 02:05 UTC — cert check window
                    mock_now = datetime(2026, 3, 13, 2, 5, 0, tzinfo=timezone.utc)
                    with patch("app.skills.wake_skill.datetime") as mock_dt:
                        mock_dt.now.return_value = mock_now
                        mock_dt.fromisoformat = datetime.fromisoformat

                        from app.skills.wake_skill import WakeSkill
                        skill = WakeSkill()
                        decision = await skill.wake()

    assert decision.action == "run_scheduled"
    assert enqueued_goals, "Should have enqueued a scheduled goal"
    cert_goals = [g for g in enqueued_goals if "cert" in g.title.lower()]
    assert cert_goals, f"Expected cert_check goal, got: {[g.title for g in enqueued_goals]}"
