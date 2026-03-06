"""
Celery tasks — the concrete work that beat schedules and workers execute.

Tasks use asyncio.run() because Celery workers are synchronous processes.
Each task has a retry policy and time limit appropriate to its workload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


# ── Scheduled tasks ───────────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.tasks.run_weekly_agent_evals",
    queue="evals",
    max_retries=2,
    default_retry_delay=300,  # 5 min between retries
    soft_time_limit=3_600,  # 1hr soft limit — logs warning
    time_limit=3_900,  # 1hr 5min hard limit — kills worker
)
def run_weekly_agent_evals(self) -> dict:
    """Run all agent quality evals and post Slack scorecard."""
    try:
        return asyncio.run(_weekly_evals())
    except Exception as exc:
        logger.error("Weekly agent eval task failed: %s", exc)
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="app.worker.tasks.run_nightly_integration_evals",
    queue="evals",
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,
    time_limit=660,
)
def run_nightly_integration_evals(self) -> dict:
    """Read-only checks of all configured integrations, post results to Slack."""
    try:
        return asyncio.run(_nightly_evals())
    except Exception as exc:
        logger.error("Nightly integration eval task failed: %s", exc)
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="app.worker.tasks.run_health_check",
    queue="celery",
    max_retries=0,  # no retries — next run is in 30 min anyway
    soft_time_limit=30,
    time_limit=45,
)
def run_health_check(self) -> dict:
    """
    Check Brain API, Redis, and Postgres health every 30 minutes.
    Posts a Slack alert to brain-alerts ONLY when something is degraded.
    """
    try:
        return asyncio.run(_health_check())
    except Exception as exc:
        logger.error("Health check task failed: %s", exc)
        return {"error": str(exc)}


# ── On-demand tasks ───────────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.worker.tasks.deploy_brain",
    queue="celery",
    max_retries=0,  # don't retry — a failed deploy needs human eyes
    soft_time_limit=360,  # 6 min soft limit
    time_limit=420,  # 7 min hard kill
)
def deploy_brain(self, reason: str = "") -> dict:
    """
    Pull latest code from GitHub → rebuild Brain Docker image → restart container.
    Runs inside the Celery worker, which has /var/run/docker.sock and /root/sentinel-workspace mounted.
    """
    try:
        return asyncio.run(_deploy_brain(reason))
    except Exception as exc:
        logger.error("deploy_brain task failed: %s", exc, exc_info=True)
        post_alert_sync(f"❌ *Brain deploy FAILED*\n`{type(exc).__name__}: {exc}`")
        return {"success": False, "error": str(exc)}


@celery_app.task(
    bind=True,
    name="app.worker.tasks.plan_and_execute_board_task",
    queue="tasks_general",
    max_retries=0,
    soft_time_limit=540,
    time_limit=600,
)
def plan_and_execute_board_task(self, task_id: int) -> dict:
    """
    Use the LLM agent loop to plan and execute a task that has no pre-defined commands.
    The agent reads files, makes edits, and commits — all autonomously.
    """
    try:
        return asyncio.run(_llm_execute_task(self.request.id or str(task_id), task_id))
    except Exception as exc:
        logger.error("plan_and_execute_board_task(%s) crashed: %s", task_id, exc, exc_info=True)
        _mark_task(task_id, "failed", str(exc))
        _dm_task_failure(task_id, f"Task #{task_id}", f"Celery crash: {exc}")
        return {"error": str(exc)}


@celery_app.task(
    bind=True,
    name="app.worker.tasks.scan_pending_tasks",
    queue="celery",
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def scan_pending_tasks(self) -> dict:
    """
    Scan for pending tasks that have never been dispatched (celery_task_id IS NULL)
    and auto-queue them based on whether they have pre-defined commands or need LLM planning.
    """
    try:
        return asyncio.run(_scan_pending_tasks())
    except Exception as exc:
        logger.error("scan_pending_tasks failed: %s", exc)
        return {"error": str(exc)}


@celery_app.task(
    bind=True,
    name="app.worker.tasks.execute_board_task",
    queue="tasks_general",  # overridden to tasks_workspace at call-time if needed
    max_retries=0,
    soft_time_limit=540,  # 9 min soft
    time_limit=600,  # 10 min hard
)
def execute_board_task(self, task_id: int) -> dict:
    """
    Execute a task from the task board.

    Workspace guardrail
    ───────────────────
    If any command in the task touches /root/sentinel-workspace, this task MUST
    run on the tasks_workspace queue (concurrency=1) AND hold the Redis workspace
    lock for the duration. This prevents concurrent writes that would cause merge
    conflicts.

    Non-workspace tasks run on tasks_general (concurrency=3) with no lock.
    """
    try:
        return asyncio.run(_execute_board_task(self.request.id or str(task_id), task_id))
    except Exception as exc:
        logger.error("execute_board_task(%s) crashed: %s", task_id, exc, exc_info=True)
        _mark_task(task_id, "failed", str(exc))
        _dm_task_failure(task_id, f"Task #{task_id}", f"Celery crash: {exc}")
        return {"error": str(exc)}


@celery_app.task(
    bind=True,
    name="app.worker.tasks.run_shell_and_report_back",
    queue="celery",
    max_retries=0,
    soft_time_limit=180,
    time_limit=210,
)
def run_shell_and_report_back(
    self,
    commands: list[str],
    channel: str,
    thread_ts: str,
    cwd: str = "/root/sentinel-workspace",
    label: str = "",
) -> dict:
    """
    Run a list of shell commands and post the result back to the originating
    Slack thread. Called when server_shell is invoked with background=true.
    """
    try:
        return asyncio.run(_shell_and_report(commands, channel, thread_ts, cwd, label))
    except Exception as exc:
        logger.error("run_shell_and_report_back failed: %s", exc, exc_info=True)
        from app.integrations.slack_notifier import post_thread_reply_sync

        post_thread_reply_sync(
            f"❌ *Background task crashed*\n`{type(exc).__name__}: {exc}`",
            channel,
            thread_ts,
        )
        return {"error": str(exc)}


@celery_app.task(
    bind=True,
    name="app.worker.tasks.investigate_and_fix_sentry_issue",
    queue="celery",
    max_retries=1,
    default_retry_delay=60,
    soft_time_limit=300,  # 5 min soft limit
    time_limit=360,  # 6 min hard kill
)
def investigate_and_fix_sentry_issue(self, task_id: int, issue_params: dict) -> dict:
    """Auto-investigate a Sentry issue and attempt a code fix via LLM + RepoClient."""
    try:
        return asyncio.run(_investigate_and_fix(task_id, issue_params))
    except Exception as exc:
        logger.error("investigate_and_fix_sentry_issue failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


# ── Async implementations ──────────────────────────────────────────────────────


async def _weekly_evals() -> dict:
    from app.evals.runner import EvalRunner
    from app.evals.reporter import post_scorecard_to_slack

    runner = EvalRunner()
    summaries = await runner.run_all_agents()

    previous: dict[str, float] = {}
    for s in summaries:
        prev = runner.get_previous_avg(s.agent_name, exclude_run_id=s.run_id)
        if prev is not None:
            previous[s.agent_name] = prev

    await post_scorecard_to_slack(summaries, previous_scores=previous)
    avg = sum(s.avg_score for s in summaries) / len(summaries) if summaries else 0.0
    logger.info("Weekly evals complete | %d agents | avg %.1f", len(summaries), avg)
    return {"agents": len(summaries), "avg_score": round(avg, 2)}


async def _nightly_evals() -> dict:
    from app.evals.integrations import run_all_integration_evals
    from app.evals.reporter import post_integration_health_to_slack

    results = await run_all_integration_evals()
    passed = sum(1 for r in results if r.passed)
    logger.info("Nightly integration evals | %d/%d passed", passed, len(results))

    # Post summary to Slack (quiet on all-green, loud on failures)
    await post_integration_health_to_slack(results)

    return {"total": len(results), "passed": passed}


async def _health_check() -> dict:
    """Check Brain API health; alert Slack if anything is down."""
    import httpx
    from app.integrations.slack_notifier import post_alert

    issues: list[str] = []

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get("http://brain:8000/api/v1/health", timeout=10)
            health = resp.json()
            if not health.get("redis"):
                issues.append("Redis is *DOWN* or unreachable")
            if not health.get("postgres"):
                issues.append("PostgreSQL is *DOWN* or unreachable")
    except Exception as exc:
        issues.append(f"Brain API unreachable: `{type(exc).__name__}: {exc}`")

    if issues:
        text = (
            "🚨 *Brain Health Alert*\n"
            + "\n".join(f"  • {i}" for i in issues)
            + "\n_Check `GET /api/v1/health` and container logs for details._"
        )
        post_alert_sync(text)
        logger.warning("Health check failed: %s", issues)
    else:
        logger.debug("Health check passed — all systems nominal")

    return {"issues": issues}


async def _shell_and_report(
    commands: list[str],
    channel: str,
    thread_ts: str,
    cwd: str,
    label: str,
) -> dict:
    """Execute commands sequentially and post a formatted result to Slack."""
    from app.integrations.slack_notifier import post_thread_reply_sync

    results: list[str] = []
    all_passed = True

    for i, cmd in enumerate(commands):
        cmd = cmd.strip()
        if not cmd:
            continue
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                executable="/bin/bash",
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = (stdout or b"").decode("utf-8", errors="replace")[:1200].strip()
            code = proc.returncode or 0
        except asyncio.TimeoutError:
            output, code = "[timed out after 120s]", -1
        except Exception as exc:
            output, code = f"[error: {exc}]", -1

        status = "✅" if code == 0 else "❌"
        snippet = f"```\n{output}\n```" if output else ""
        results.append(f"{status} *Step {i + 1}:* `{cmd}`\n{snippet}".strip())
        if code != 0:
            all_passed = False
            break

    header = (
        f"✅ *{label or 'Background task'} — complete*"
        if all_passed
        else f"❌ *{label or 'Background task'} — failed at step {len(results)}*"
    )
    divider = "─" * 36
    body = f"\n{divider}\n".join(results)
    post_thread_reply_sync(f"{header}\n{divider}\n{body}", channel, thread_ts)

    return {"passed": all_passed, "steps": len(results)}


def _touches_workspace(commands: list[str]) -> bool:
    """Return True if any command references /root/sentinel-workspace."""
    return any("/root/sentinel-workspace" in (c or "") for c in commands)


def _mark_task(task_id: int, status: str, error: str | None = None) -> None:
    """Update a task board row status synchronously (safe from Celery).

    When status is 'done', also auto-enqueue any tasks whose blocked_by list
    contained this task_id and are now fully unblocked.
    When status is terminal (done/failed), clear celery_task_id so the task
    can be re-dispatched if the user resets it to pending.
    """
    try:
        from app.db import postgres

        # Clear celery_task_id on terminal states so tasks can be retried
        if status in ("done", "failed"):
            postgres.execute(
                "UPDATE tasks SET status=%s, celery_task_id=NULL, updated_at=NOW() WHERE id=%s",
                (status, task_id),
            )
        else:
            postgres.execute(
                "UPDATE tasks SET status=%s, updated_at=NOW() WHERE id=%s",
                (status, task_id),
            )
        if error:
            logger.warning("Task #%s marked %s — %s", task_id, status, error[:200])
    except Exception as exc:
        logger.warning("Could not update task #%s status: %s", task_id, exc)

    # Post status update to sentinel-tasks thread
    # in_progress: always notify  |  failed with error: crash notify (report posted separately for normal done/failed)
    if status == "in_progress":
        try:
            from app.integrations.task_notifier import notify_status_sync, _get_task_title

            notify_status_sync(task_id, _get_task_title(task_id), "in_progress")
        except Exception as _nte:
            logger.debug("sentinel-tasks notify failed: %s", _nte)
    elif status == "failed" and error:
        # Crash before execution report — post a minimal failure notice
        try:
            from app.integrations.task_notifier import notify_report_sync, _get_task_title

            notify_report_sync(task_id, _get_task_title(task_id), False, f"```{error[:600]}```")
        except Exception as _nte:
            logger.debug("sentinel-tasks crash notify failed: %s", _nte)

    # On completion, unblock dependent tasks
    if status == "done":
        _unblock_dependents(task_id)


def _dm_task_failure(task_id: int, title: str, summary: str) -> None:
    """DM the owner when a task fails and no Slack thread is available."""
    try:
        from app.config import get_settings as _gs

        _s = _gs()
        if not (_s.slack_owner_user_id and _s.slack_bot_token):
            return
        from app.integrations.slack_notifier import post_dm_sync

        short = summary[:600] if summary else "(no details)"
        post_dm_sync(
            f"❌ *Task #{task_id} — {title}* failed\n```{short}```"
        )
    except Exception as exc:
        logger.warning("_dm_task_failure could not send DM: %s", exc)


def _unblock_dependents(completed_task_id: int) -> None:
    """Find tasks blocked by completed_task_id and auto-enqueue those now fully unblocked."""
    try:
        from app.db import postgres

        # Find all pending tasks that list completed_task_id in their blocked_by array
        dependents = postgres.execute(
            """
            SELECT id, execution_queue, commands, approval_level,
                   blocked_by
            FROM   tasks
            WHERE  status = 'pending'
              AND  blocked_by @> %s::jsonb
            """,
            (json.dumps([completed_task_id]),),
        )
        if not dependents:
            return

        for dep in dependents:
            dep_id = dep["id"]
            # Check if ALL blockers are now done
            raw_blocked = dep.get("blocked_by") or []
            if isinstance(raw_blocked, str):
                raw_blocked = json.loads(raw_blocked)
            blocker_ids: list[int] = [int(x) for x in raw_blocked if x]

            if blocker_ids:
                statuses = postgres.execute(
                    "SELECT id, status FROM tasks WHERE id = ANY(%s)",
                    (blocker_ids,),
                )
                still_blocked = any(r["status"] != "done" for r in statuses)
                if still_blocked:
                    logger.info(
                        "Task #%s still blocked after #%s completed",
                        dep_id,
                        completed_task_id,
                    )
                    continue

            # All blockers done — enqueue if it has commands and approval_level == 1
            raw_cmds = dep.get("commands") or []
            if isinstance(raw_cmds, str):
                raw_cmds = json.loads(raw_cmds)
            if raw_cmds and dep.get("approval_level", 2) == 1:
                q = dep.get("execution_queue") or "tasks_general"
                result = execute_board_task.apply_async(args=[dep_id], queue=q)
                postgres.execute(
                    "UPDATE tasks SET celery_task_id=%s WHERE id=%s",
                    (result.id, dep_id),
                )
                logger.info("Auto-enqueued task #%s (unblocked by #%s)", dep_id, completed_task_id)
            else:
                logger.info(
                    "Task #%s unblocked by #%s but not auto-queued (no commands or approval_level>1)",
                    dep_id,
                    completed_task_id,
                )
    except Exception as exc:
        logger.warning("Could not unblock dependents of task #%s: %s", completed_task_id, exc)


async def _execute_board_task(celery_task_id: str, task_id: int) -> dict:
    """
    Core logic for execute_board_task.

    1. Load the task row from the DB (needs commands + slack context).
    2. If commands touch the workspace, acquire the Redis workspace lock.
       Retry up to 10 times (every 30s) before giving up.
    3. Run commands sequentially via _run_command.
    4. Post results back to the originating Slack thread (if context stored).
    5. Release the lock and update task status.
    """
    import json as _json
    from app.db import postgres
    from app.memory.redis_client import RedisMemory
    from app.integrations.slack_notifier import post_thread_reply_sync

    redis = RedisMemory()

    # ── 1. Load task ──────────────────────────────────────────────────────────
    row = postgres.execute_one(
        "SELECT id, title, commands, slack_channel, slack_thread_ts, session_id, "
        "       blocked_by, approval_level "
        "FROM tasks WHERE id = %s",
        (task_id,),
    )
    if not row:
        return {"error": f"Task #{task_id} not found"}

    title = row.get("title", f"Task #{task_id}")
    raw_cmds = row.get("commands") or []
    commands: list[str] = (
        raw_cmds if isinstance(raw_cmds, list) else _json.loads(raw_cmds) if isinstance(raw_cmds, str) else []
    )
    channel = row.get("slack_channel") or ""
    thread_ts = row.get("slack_thread_ts") or ""
    session_id = row.get("session_id") or ""
    approval_level = row.get("approval_level") or 1

    # ── 1b. blocked_by check ──────────────────────────────────────────────────
    raw_blocked = row.get("blocked_by") or []
    if isinstance(raw_blocked, str):
        raw_blocked = _json.loads(raw_blocked)
    blocker_ids: list[int] = [int(x) for x in raw_blocked if x]

    if blocker_ids:
        blocker_rows = postgres.execute(
            "SELECT id, status FROM tasks WHERE id = ANY(%s)",
            (blocker_ids,),
        )
        incomplete = [r["id"] for r in blocker_rows if r["status"] != "done"]
        if incomplete:
            logger.info(
                "Task #%s blocked by task(s) %s — re-queuing in 30s (attempt via Celery retry)",
                task_id,
                incomplete,
            )
            # Re-queue with a 30s countdown instead of executing now
            execute_board_task.apply_async(args=[task_id], countdown=30)
            return {"status": "requeued", "blocked_by": incomplete}

    # ── 1c. approval_level gate ───────────────────────────────────────────────
    # Tasks with approval_level >= 2 must not auto-run unless manually approved.
    # (Approved tasks arrive here via the task board PATCH endpoint which sets
    # status=in_progress; at that point approval_level can be ignored.)
    # We only block tasks that are still in 'pending' status here.
    pending_status_row = postgres.execute_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    current_status = (pending_status_row or {}).get("status", "pending")
    if approval_level >= 2 and current_status == "pending":
        from app.config import get_settings as _gs

        _s = _gs()
        if _s.slack_owner_user_id and _s.slack_bot_token:
            from app.integrations.slack_notifier import post_dm_sync, post_alert_sync as _pas

            _domain = _s.domain or "sentinelai.cloud"
            _dm = (
                f"🔐 *Approval needed — Task #{task_id}: {title}*\n"
                f"Approval level: {'requires sign-off' if approval_level == 3 else 'needs review'}\n\n"
                f"To approve, PATCH the task to in_progress:\n"
                f"`PATCH https://{_domain}/api/v1/board/tasks/{task_id}` "
                f'with `{{"status": "in_progress"}}`\n\n'
                "Or reply *confirm* in the originating Slack thread."
            )
            post_dm_sync(_dm)
            _pas(
                f"🔐 *Approval needed — Task #{task_id}: {title}*\nApproval level {approval_level} — DM sent to owner."
            )
        _mark_task(task_id, "pending")  # stays pending until approved
        return {"status": "awaiting_approval", "approval_level": approval_level}

    # Fall back to session-keyed Slack context if not stored on the task itself
    if (not channel or not thread_ts) and session_id:
        ctx = redis.get_slack_context(session_id)
        if ctx:
            channel = ctx.get("channel", "")
            thread_ts = ctx.get("thread_ts", "")

    if not commands:
        # No pre-defined commands — hand off to the LLM agent loop
        logger.info("Task #%s has no commands — routing to LLM agent loop", task_id)
        return await _llm_execute_task(celery_task_id, task_id)

    needs_lock = _touches_workspace(commands)

    # ── 2. Acquire workspace lock (workspace tasks only) ──────────────────────
    if needs_lock:
        acquired = False
        for attempt in range(10):
            if redis.acquire_workspace_lock(celery_task_id):
                acquired = True
                break
            holder = redis.get_workspace_lock_holder()
            logger.info(
                "Task #%s waiting for workspace lock (held by %s) — attempt %d/10",
                task_id,
                holder,
                attempt + 1,
            )
            await asyncio.sleep(30)

        if not acquired:
            _mark_task(task_id, "failed", "could not acquire workspace lock after 5 min")
            if channel and thread_ts:
                post_thread_reply_sync(
                    f"⏳ *Task #{task_id} — {title}*\n"
                    "Could not start — workspace was busy for 5 minutes. "
                    "The task has been marked failed; create it again to retry.",
                    channel,
                    thread_ts,
                )
            return {"error": "workspace_lock_timeout"}

    # ── 3. Mark in_progress ───────────────────────────────────────────────────
    _mark_task(task_id, "in_progress")

    # ── 4. Run commands ───────────────────────────────────────────────────────
    cwd = "/root/sentinel-workspace" if needs_lock else "/root"
    results: list[str] = []
    all_passed = True

    for i, cmd in enumerate(commands):
        cmd = cmd.strip()
        if not cmd:
            continue
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                executable="/bin/bash",
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = (stdout or b"").decode("utf-8", errors="replace")[:1_200].strip()
            code = proc.returncode or 0
        except asyncio.TimeoutError:
            output, code = "[timed out after 120s]", -1
        except Exception as exc:
            output, code = f"[error: {exc}]", -1

        status = "✅" if code == 0 else "❌"
        snippet = f"```\n{output}\n```" if output else ""
        results.append(f"{status} *Step {i + 1}:* `{cmd}`\n{snippet}".strip())
        if code != 0:
            all_passed = False
            break

    # ── 5. Release lock ───────────────────────────────────────────────────────
    if needs_lock:
        redis.release_workspace_lock(celery_task_id)

    # ── 6. Update task status ─────────────────────────────────────────────────
    _mark_task(task_id, "done" if all_passed else "failed")

    try:
        from app.integrations.milestone_logger import log_milestone
        import asyncio as _asyncio
        _asyncio.create_task(log_milestone(
            action="task_complete" if all_passed else "task_failed",
            intent="task_execute",
            params={"title": title},
            session_id=session_id or f"task-{task_id}",
            detail={"task_id": task_id, "title": title, "steps": len(results), "passed": all_passed},
            agent="celery",
        ))
    except Exception:
        pass

    # ── 7. Report back to Slack ───────────────────────────────────────────────
    divider = "─" * 36
    report_body = f"\n{divider}\n".join(results) if results else "(no steps recorded)"

    if channel and thread_ts:
        header = (
            f"✅ *Task #{task_id} — {title}* — complete"
            if all_passed
            else f"❌ *Task #{task_id} — {title}* — failed at step {len(results)}"
        )
        post_thread_reply_sync(f"{header}\n{divider}\n{report_body}", channel, thread_ts)
    elif not all_passed:
        _dm_task_failure(task_id, title, report_body)

    # ── 8. Report to sentinel-tasks channel thread ─────────────────────────────
    try:
        from app.integrations.task_notifier import notify_report_sync

        notify_report_sync(task_id, title, all_passed, report_body)
    except Exception as _nte:
        logger.debug("sentinel-tasks report failed: %s", _nte)

    return {"task_id": task_id, "passed": all_passed, "steps": len(results)}


async def _llm_execute_task(celery_task_id: str, task_id: int) -> dict:
    """
    Agentic loop for tasks with no pre-defined commands.

    Each round asks the LLM for the next shell command to run.  The LLM sees
    the full command history so far and decides whether to run another command
    or declare the task done / failed.  Supports up to 8 rounds.

    LLM response schema (strict JSON, one of):
      {"command": "...", "reasoning": "..."}
      {"done": true, "summary": "..."}
      {"done": true, "failed": true, "summary": "..."}
    """
    import anthropic
    from app.config import get_settings
    from app.db import postgres
    from app.memory.redis_client import RedisMemory
    from app.integrations.slack_notifier import post_thread_reply_sync

    settings = get_settings()
    redis = RedisMemory()
    code_root = "/root/sentinel-workspace" if os.path.isdir("/root/sentinel-workspace") else "/app"

    # ── Load task ─────────────────────────────────────────────────────────────
    row = postgres.execute_one(
        "SELECT id, title, description, slack_channel, slack_thread_ts, session_id, "
        "       blocked_by, approval_level "
        "FROM tasks WHERE id = %s",
        (task_id,),
    )
    if not row:
        return {"error": f"Task #{task_id} not found"}

    title = row.get("title", f"Task #{task_id}")
    description = row.get("description") or ""
    channel = row.get("slack_channel") or ""
    thread_ts = row.get("slack_thread_ts") or ""
    session_id = row.get("session_id") or ""

    # blocked_by check
    import json as _json

    raw_blocked = row.get("blocked_by") or []
    if isinstance(raw_blocked, str):
        raw_blocked = _json.loads(raw_blocked)
    blocker_ids = [int(x) for x in raw_blocked if x]
    if blocker_ids:
        blocker_rows = postgres.execute("SELECT id, status FROM tasks WHERE id = ANY(%s)", (blocker_ids,))
        incomplete = [r["id"] for r in blocker_rows if r["status"] != "done"]
        if incomplete:
            execute_board_task.apply_async(args=[task_id], countdown=30)
            return {"status": "requeued", "blocked_by": incomplete}

    # approval_level gate
    approval_level = row.get("approval_level") or 1
    pending_row = postgres.execute_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    current_status = (pending_row or {}).get("status", "pending")
    if approval_level >= 2 and current_status == "pending":
        from app.config import get_settings as _gs

        _s = _gs()
        if _s.slack_owner_user_id and _s.slack_bot_token:
            from app.integrations.slack_notifier import post_dm_sync, post_alert_sync as _pas

            _domain = _s.domain or "sentinelai.cloud"
            _dm = (
                f"🔐 *Approval needed — Task #{task_id}: {title}*\n"
                f"PATCH https://{_domain}/api/v1/board/tasks/{task_id} "
                '`{"status": "in_progress"}` to approve.'
            )
            post_dm_sync(_dm)
        _mark_task(task_id, "pending")
        return {"status": "awaiting_approval", "approval_level": approval_level}

    _mark_task(task_id, "in_progress")

    if channel and thread_ts:
        post_thread_reply_sync(
            f"🧠 *Task #{task_id} — {title}*\n_Planning and executing autonomously..._",
            channel,
            thread_ts,
        )

    # ── Sync workspace to latest origin/main and create a sentinel branch ──────
    import datetime as _dt
    sentinel_branch = f"sentinel/task-{task_id}-{_dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    sync_output = ""
    if os.path.isdir(os.path.join(code_root, ".git")):
        try:
            sync_proc = await asyncio.create_subprocess_shell(
                f"git -C {code_root} fetch origin && "
                f"git -C {code_root} checkout -B {sentinel_branch} origin/main",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                executable="/bin/bash",
            )
            sync_stdout, _ = await asyncio.wait_for(sync_proc.communicate(), timeout=60)
            sync_output = (sync_stdout or b"").decode("utf-8", errors="replace").strip()
            logger.info("Task #%s on branch %s: %s", task_id, sentinel_branch, sync_output[:120])
        except Exception as sync_exc:
            logger.warning("Task #%s workspace sync failed: %s", task_id, sync_exc)

    # ── Agent loop ────────────────────────────────────────────────────────────
    system_prompt = (
        "You are Sentinel, an autonomous AI agent executing server tasks via shell commands.\n"
        f"Workspace: {code_root}  (a git repository)\n"
        f"You are working on branch: {sentinel_branch}\n\n"
        "Each response must be ONLY a JSON object — one of:\n"
        '  {"command": "<bash command>", "reasoning": "<why>"}\n'
        '  {"done": true, "summary": "<what was accomplished>"}\n'
        '  {"done": true, "failed": true, "summary": "<why it failed>"}\n\n'
        "Rules:\n"
        f"- Use absolute paths starting with {code_root}/\n"
        "- JSON file edits: python3 -c with json.load/json.dump\n"
        f"- After any file change: git -C {code_root} add -A && "
        f"  git -C {code_root} commit -m '<msg>' && "
        f"  git -C {code_root} push origin {sentinel_branch}\n"
        f"- NEVER push to main or master — always push to {sentinel_branch}\n"
        "- A PR will be opened automatically after you push for human review before deploy\n"
        "- Max one command per response\n"
        "- Limit research to at most 4 rounds — then start implementing\n"
        "- Once you understand the codebase, make changes immediately\n"
        "- Never ask questions — decide autonomously\n"
        "- No markdown in your response — pure JSON only"
    )

    messages: list[dict] = [
        {"role": "user", "content": f"Task #{task_id}: {title}\nDescription: {description or '(none)'}"}
    ]

    results: list[str] = []
    all_passed = True
    lm_said_done = False
    workspace_lock_held = False
    max_rounds = 20
    round_num = 0
    parse_errors = 0
    write_commands_run = 0  # track how many write/edit commands have been issued
    consecutive_read_rounds = 0  # track read-only rounds in a row

    _READ_ONLY_PREFIXES = ("cat ", "grep ", "find ", "ls ", "head ", "tail ", "wc ", "echo ")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    for round_num in range(max_rounds):
        # Warn LLM when nearing the round limit
        if round_num == max_rounds - 3:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"⚠️ You have {max_rounds - round_num} rounds remaining. "
                        "If the task is complete or cannot be completed, return "
                        '{"done": true, "summary": "..."} now.'
                    ),
                }
            )

        # Ask LLM for the next action
        try:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=system_prompt,
                messages=messages,
            )
            raw = resp.content[0].text.strip()
            # Strip non-printable / null bytes that fool the truthiness check
            raw = "".join(ch for ch in raw if ch.isprintable() or ch in "\n\r\t")
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            if not raw.strip():
                raise ValueError("LLM returned empty response")
            action = json.loads(raw)
            parse_errors = 0  # reset on success
        except Exception as exc:
            parse_errors += 1
            logger.error("LLM round %d failed for task #%s: %s", round_num, task_id, exc)
            if parse_errors >= 3:
                all_passed = False
                results.append(
                    f"❌ *LLM parse error (round {round_num + 1}):* {exc} — aborting after 3 consecutive errors"
                )
                break
            # inject a correction hint and retry
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your last response could not be parsed as JSON (error: {exc}). "
                        "Reply with ONLY a valid JSON object — no markdown, no explanation."
                    ),
                }
            )
            continue

        if action.get("done"):
            lm_said_done = True
            summary = action.get("summary", "Task completed")
            icon = "❌" if action.get("failed") else "✅"
            results.append(f"{icon} *Summary:* {summary}")
            if action.get("failed"):
                all_passed = False
            break

        cmd = (action.get("command") or "").strip()
        if not cmd:
            # LLM returned neither done nor a command — treat as done-with-failure
            results.append("❌ *LLM returned no command and did not mark done — aborting*")
            all_passed = False
            break

        # Acquire workspace lock on first workspace-touching command
        if not workspace_lock_held and code_root in cmd:
            acquired = False
            for _ in range(10):
                if redis.acquire_workspace_lock(celery_task_id):
                    acquired = True
                    workspace_lock_held = True
                    break
                await asyncio.sleep(30)
            if not acquired:
                all_passed = False
                results.append("❌ Could not acquire workspace lock after 5 minutes")
                break

        cwd = code_root if workspace_lock_held else "/root"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                executable="/bin/bash",
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = (stdout or b"").decode("utf-8", errors="replace")[:2_000].strip()
            exit_code = proc.returncode or 0
        except asyncio.TimeoutError:
            output, exit_code = "[timed out after 120s]", -1
        except Exception as exc:
            output, exit_code = f"[error: {exc}]", -1

        # Track write activity
        cmd_stripped = cmd.lstrip()
        is_read_only = any(cmd_stripped.startswith(p) for p in _READ_ONLY_PREFIXES)
        if not is_read_only:
            write_commands_run += 1
            consecutive_read_rounds = 0
        else:
            consecutive_read_rounds += 1

        status_icon = "✅" if exit_code == 0 else "❌"
        snippet = f"```\n{output}\n```" if output else ""
        results.append(f"{status_icon} *Round {round_num + 1}:* `{cmd[:120]}`\n{snippet}".strip())
        logger.info(
            "Task #%s round %d/%d exit=%d cmd=%s",
            task_id,
            round_num + 1,
            max_rounds,
            exit_code,
            cmd[:80],
        )

        # Inject action-pressure message if stuck in read-only loop
        action_pressure = ""
        if consecutive_read_rounds >= 5:
            action_pressure = (
                f"\n\n⚠️ You have run {consecutive_read_rounds} consecutive read-only commands. "
                "STOP exploring. You now have enough context. "
                "Execute the task: run scripts, call APIs, write output, or mark done/failed. "
                "Do not run any more cat/grep/ls/head/tail commands."
            )
        elif round_num >= 4 and write_commands_run == 0:
            action_pressure = (
                "\n\n⚠️ You have been researching for several rounds without taking action. "
                "Execute the task now — run scripts, call APIs, write/edit files, or mark done/failed."
            )

        # Feed result back to LLM
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Command output (exit {exit_code}):\n```\n{output[:1_500]}\n```\n"
                    + (
                        "Command succeeded. Continue with next step or mark done."
                        if exit_code == 0
                        else "Command FAILED. You may retry, try an alternative, or mark done/failed."
                    )
                    + action_pressure
                ),
            }
        )

        if exit_code != 0 and round_num >= max_rounds - 2:
            all_passed = False
            break

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if workspace_lock_held:
        redis.release_workspace_lock(celery_task_id)

    # ── Safety push: ensure any file changes reach GitHub to trigger CI/CD ───
    # Runs after the agent loop regardless of whether the LLM remembered to push.
    if write_commands_run > 0 and os.path.isdir(os.path.join(code_root, ".git")):
        try:
            push_proc = await asyncio.create_subprocess_shell(
                # Commit any stragglers, then push branch (never push to main)
                f"cd {code_root} && "
                f"git add -A && "
                f"(git diff --cached --quiet || git commit -m 'chore: task #{task_id} — final auto-commit') && "
                f"git push origin {sentinel_branch} --set-upstream",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                executable="/bin/bash",
            )
            push_out, _ = await asyncio.wait_for(push_proc.communicate(), timeout=60)
            push_log = (push_out or b"").decode("utf-8", errors="replace").strip()
            logger.info("Task #%s safety push: %s", task_id, push_log[:200])
            results.append(f"🚀 *GitHub push:* `{push_log[:120]}`")
        except Exception as push_exc:
            logger.warning("Task #%s safety push failed: %s", task_id, push_exc)

    # ── Open PR if commits were pushed to the sentinel branch ─────────────────
    pr_url = ""
    try:
        check_proc = await asyncio.create_subprocess_shell(
            f"git -C {code_root} log origin/main..{sentinel_branch} --oneline 2>/dev/null | wc -l",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            executable="/bin/bash",
        )
        count_out, _ = await asyncio.wait_for(check_proc.communicate(), timeout=15)
        ahead = int((count_out or b"0").decode().strip())
        if ahead > 0:
            from app.integrations.repo import _open_pr_sync, _notify_pr_slack

            pr_url, pr_number = await asyncio.to_thread(
                _open_pr_sync,
                sentinel_branch,
                f"sentinel: task #{task_id} — {title[:80]}",
                f"Automated changes from Sentinel task #{task_id}.\n\n"
                f"**Task:** {title}\n\n"
                "**Review carefully before merging.** Merging triggers CI and deploys to production.",
            )
            if pr_number:
                _notify_pr_slack(pr_url, sentinel_branch, pr_number)
                results.append(f"🔀 *PR opened:* {pr_url}")
                logger.info("Task #%s PR opened: %s", task_id, pr_url)
    except Exception as pr_exc:
        logger.warning("Task #%s PR creation failed: %s", task_id, pr_exc)

    # Only mark done if LLM explicitly said done AND all commands passed.
    # If we exhausted max_rounds without the LLM saying done, mark failed.
    if not lm_said_done and all_passed:
        all_passed = False
        results.append(f"❌ *Agent exhausted {max_rounds} rounds without explicitly completing the task*")

    _mark_task(task_id, "done" if all_passed else "failed")

    try:
        from app.integrations.milestone_logger import log_milestone
        import asyncio as _asyncio
        _asyncio.create_task(log_milestone(
            action="task_complete" if all_passed else "task_failed",
            intent="task_execute",
            params={"title": title},
            session_id=session_id or f"task-{task_id}",
            detail={"task_id": task_id, "title": title, "rounds": round_num + 1, "passed": all_passed},
            agent="celery",
        ))
    except Exception:
        pass

    divider = "─" * 36
    report_body = f"\n{divider}\n".join(results) if results else "(no steps recorded)"

    if channel and thread_ts:
        header = (
            f"✅ *Task #{task_id} — {title}* — complete" if all_passed else f"❌ *Task #{task_id} — {title}* — failed"
        )
        post_thread_reply_sync(f"{header}\n{divider}\n{report_body}", channel, thread_ts)
    elif not all_passed:
        _dm_task_failure(task_id, title, report_body)

    # Report to sentinel-tasks channel thread
    try:
        from app.integrations.task_notifier import notify_report_sync

        notify_report_sync(task_id, title, all_passed, report_body, pr_url)
    except Exception as _nte:
        logger.debug("sentinel-tasks report failed: %s", _nte)

    return {"task_id": task_id, "passed": all_passed, "rounds": round_num + 1}


async def _scan_pending_tasks() -> dict:
    """
    Find all pending tasks that:
      - Have approval_level == 1 (auto-approve)
      - Have never been dispatched (celery_task_id IS NULL)
      - Are not blocked by other tasks

    Tasks with pre-defined commands go to execute_board_task.
    Tasks without commands go to plan_and_execute_board_task (LLM agent loop).
    """
    import json as _json
    from app.db import postgres

    rows = postgres.execute(
        """
        SELECT id, title, commands, execution_queue, blocked_by
        FROM   tasks
        WHERE  status        = 'pending'
          AND  approval_level = 1
          AND  (celery_task_id IS NULL OR celery_task_id = '')
        ORDER BY COALESCE(priority_num, 3) DESC, created_at ASC
        LIMIT 10
        """,
    )

    dispatched = 0
    for row in rows or []:
        task_id = row["id"]

        # Skip blocked tasks
        raw_blocked = row.get("blocked_by") or []
        if isinstance(raw_blocked, str):
            raw_blocked = _json.loads(raw_blocked) if raw_blocked else []
        if raw_blocked:
            blocker_ids = [int(x) for x in raw_blocked if x]
            if blocker_ids:
                blocker_rows = postgres.execute("SELECT status FROM tasks WHERE id = ANY(%s)", (blocker_ids,))
                if any(r["status"] != "done" for r in (blocker_rows or [])):
                    logger.info("scan_pending_tasks: task #%s still blocked — skipping", task_id)
                    continue

        raw_cmds = row.get("commands") or []
        if isinstance(raw_cmds, str):
            raw_cmds = _json.loads(raw_cmds) if raw_cmds else []
        commands = [c for c in raw_cmds if c and c.strip()]

        q = row.get("execution_queue") or "tasks_general"
        if commands:
            result = execute_board_task.apply_async(args=[task_id], queue=q)
        else:
            result = plan_and_execute_board_task.apply_async(args=[task_id], queue=q)

        postgres.execute(
            "UPDATE tasks SET celery_task_id=%s WHERE id=%s",
            (result.id, task_id),
        )
        task_type = "cmd" if commands else "llm"
        logger.info(
            "scan_pending_tasks: dispatched task #%s (%s) → %s celery_id=%s",
            task_id,
            task_type,
            q,
            result.id[:8],
        )
        dispatched += 1

    return {"dispatched": dispatched}


def post_alert_sync(text: str) -> None:
    """Fire-and-forget sync Slack alert (used inside Celery tasks)."""
    from app.integrations.slack_notifier import post_alert_sync as _pas

    _pas(text)


async def _deploy_brain(reason: str) -> dict:
    """
    1. Post 'deploy started' to Slack.
    2. Sleep 5 s — gives the brain time to finish sending its response.
    3. git pull origin main  (updates /root/sentinel-workspace = /root/sentinel on host).
    4. docker compose build brain.
    5. docker compose up -d brain  (hot-swap with new image).
    6. Post result to Slack.
    """
    from app.integrations.slack_notifier import post_alert_sync as _notify

    project_dir = "/root/sentinel-workspace"
    compose_file = f"{project_dir}/docker-compose.yml"
    steps: list[str] = []
    errors: list[str] = []

    _notify(
        f"🔄 *Brain deploy started*\n"
        f"Reason: _{reason or 'manual request'}_\n"
        "_Pulling latest code and rebuilding image..._"
    )

    await asyncio.sleep(5)  # let the brain finish its HTTP/Slack response

    # ── 1. git pull ───────────────────────────────────────────────────────────
    try:
        env = {**os.environ, "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=no"}
        r = subprocess.run(
            ["git", "-C", project_dir, "pull", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if r.returncode == 0:
            steps.append(f"git pull: {r.stdout.strip() or 'already up to date'}")
            logger.info("Deploy git pull OK: %s", r.stdout.strip())
        else:
            errors.append(f"git pull failed: {r.stderr.strip()}")
            logger.error("Deploy git pull failed: %s", r.stderr.strip())
    except Exception as exc:
        errors.append(f"git pull error: {exc}")
        logger.error("Deploy git pull exception: %s", exc)

    if errors:
        _notify(f"❌ *Brain deploy aborted — git pull failed*\n`{errors[0][:300]}`")
        return {"success": False, "step": "git_pull", "errors": errors}

    # ── 2. docker compose build brain ────────────────────────────────────────
    try:
        r = subprocess.run(
            ["docker", "compose", "-p", "sentinel", "-f", compose_file, "build", "brain"],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "DOCKER_BUILDKIT": "1"},
        )
        if r.returncode == 0:
            steps.append("docker compose build: success")
            logger.info("Deploy image build OK")
        else:
            err = (r.stderr or r.stdout)[-600:].strip()
            errors.append(f"build failed: {err}")
            logger.error("Deploy build failed: %s", err)
    except Exception as exc:
        errors.append(f"build error: {exc}")
        logger.error("Deploy build exception: %s", exc)

    if errors:
        _notify(f"❌ *Brain deploy failed — image build error*\n```{errors[-1][:400]}```")
        return {"success": False, "step": "docker_build", "steps": steps, "errors": errors}

    # ── 3. docker compose up -d brain ────────────────────────────────────────
    # --no-deps: only restart brain, don't touch postgres/redis/qdrant etc.
    # -p sentinel: match the project name used when the stack was first started.
    try:
        r = subprocess.run(
            ["docker", "compose", "-p", "sentinel", "-f", compose_file, "up", "-d", "--no-deps", "brain"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode == 0:
            steps.append("docker compose up -d brain: success")
            logger.info("Deploy container restart OK")
        else:
            err = (r.stderr or r.stdout)[-400:].strip()
            errors.append(f"restart failed: {err}")
            logger.error("Deploy restart failed: %s", err)
    except Exception as exc:
        errors.append(f"restart error: {exc}")
        logger.error("Deploy restart exception: %s", exc)

    if errors:
        _notify(f"❌ *Brain deploy failed — container restart error*\n```{errors[-1][:400]}```")
        return {"success": False, "step": "docker_up", "steps": steps, "errors": errors}

    _notify(
        "✅ *Brain deploy complete!*\n"
        "_New image is running. Brain should be back online within a few seconds._\n"
        f"Steps: {' → '.join(steps)}"
    )
    return {"success": True, "steps": steps}


async def _investigate_and_fix(task_id: int, issue_params: dict) -> dict:
    """
    1. Mark task in_progress + post "starting" Slack message.
    2. Fetch stack-trace context from Sentry API.
    3. Ask LLM (claude-haiku) for a structured fix plan as JSON.
    4. If fixable: create a sentinel/sentry-* branch, apply patches, commit, push → PR.
    5. Resolve in Sentry if patch fully addresses the root cause.
    6. Post Slack summary (root cause + fix status + PR link).
    7. Mark task done / failed.
    """
    from app.config import get_settings
    from app.integrations.slack_notifier import post_alert, post_alert_sync

    settings = get_settings()

    issue_id = issue_params.get("issue_id", "")
    title = issue_params.get("title", "Unknown error")
    level = issue_params.get("level", "error")
    project = issue_params.get("project", "")
    permalink = issue_params.get("permalink", "")
    rank = issue_params.get("rank", "")
    badge = "🔴" if level in ("fatal", "critical") else "🟠" if level == "error" else "🟡"

    # ── 1. Mark in_progress + Slack start notification ─────────────────────────
    _mark_task(task_id, "in_progress")
    try:
        link_text = f"<{permalink}|{issue_id}>" if permalink else f"`{issue_id}`"
        post_alert_sync(
            f"{badge} *Investigating Sentry issue* — task #{task_id}"
            + (f" (#{rank})" if rank else "")
            + f"\n*{title[:120]}*"
            + f"\nProject: {project} | Level: {level} | Sentry: {link_text}"
        )
    except Exception:
        pass

    # ── 2. Fetch rich Sentry event via API ────────────────────────────────────
    issue_context = ""
    # Track which files+lines the Sentry API tells us are relevant so we can
    # extract the exact function from the repo, not just dump the whole file.
    _sentry_frames: list[dict] = []  # [{filename, lineno, function}]
    _third_party_lib: str = ""       # top-level lib name when error is in a dependency

    if settings.sentry_auth_token and issue_id:
        try:
            import httpx

            headers = {"Authorization": f"Bearer {settings.sentry_auth_token}"}
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"https://sentry.io/api/0/issues/{issue_id}/events/latest/",
                    headers=headers,
                    timeout=15,
                )
            if resp.status_code == 200:
                event = resp.json()

                # Exception chain
                for entry in event.get("entries", []):
                    if entry.get("type") != "exception":
                        continue
                    for exc_val in (entry.get("data") or {}).get("values", [])[:2]:
                        exc_type = exc_val.get("type", "")
                        exc_value = exc_val.get("value", "")
                        issue_context += f"Exception: {exc_type}: {exc_value}\n"

                        frames = (exc_val.get("stacktrace") or {}).get("frames", [])
                        # Last 8 frames — deepest call is most relevant for context
                        for frame in frames[-8:]:
                            fname = frame.get("filename", "")
                            lineno = frame.get("lineno", 0)
                            func = frame.get("function", "")
                            ctx = (frame.get("context_line") or "").strip()
                            pre = frame.get("pre_context") or []
                            post = frame.get("post_context") or []
                            lvars = frame.get("vars") or {}

                            issue_context += f"\n  File {fname}:{lineno} in {func}()\n"
                            # Sentry gives ±5 surrounding lines — include them all
                            for l in pre:
                                issue_context += f"    {l}\n"
                            if ctx:
                                issue_context += f" →  {ctx}\n"
                            for l in post:
                                issue_context += f"    {l}\n"
                            # Local variables at time of crash
                            if lvars:
                                trimmed = {k: str(v)[:120] for k, v in list(lvars.items())[:8]}
                                issue_context += f"    locals: {json.dumps(trimmed)}\n"

                            if fname.startswith("app/"):
                                _sentry_frames.append({"filename": fname, "lineno": lineno})

                        # Also scan frames earlier in the call stack for app/ entry points.
                        # When the error is deep in a third-party library our code appears
                        # at the beginning of the stack, not the end.
                        for frame in (frames[:-8] if len(frames) > 8 else []):
                            fname = frame.get("filename", "")
                            if fname.startswith("app/") and not any(
                                f["filename"] == fname for f in _sentry_frames
                            ):
                                _sentry_frames.append(
                                    {"filename": fname, "lineno": frame.get("lineno", 0)}
                                )

                        # Identify the top-level third-party library for caller discovery
                        if not _third_party_lib:
                            for frame in reversed(frames):
                                fname = frame.get("filename", "")
                                if fname and not fname.startswith("app/") and "/" in fname:
                                    lib = fname.split("/")[0]
                                    if lib and lib not in ("", "."):
                                        _third_party_lib = lib
                                        break

                # Breadcrumbs — last 10 give execution path leading to the crash
                for entry in event.get("entries", []):
                    if entry.get("type") != "breadcrumbs":
                        continue
                    crumbs = (entry.get("data") or {}).get("values", [])[-10:]
                    if crumbs:
                        issue_context += "\nBreadcrumbs (last 10 before crash):\n"
                        for c in crumbs:
                            ts = (c.get("timestamp") or "")[:19]
                            cat = c.get("category", "")
                            msg = (c.get("message") or c.get("data", {}).get("url", ""))[:120]
                            issue_context += f"  [{ts}] {cat}: {msg}\n"

            elif resp.status_code == 404:
                logger.info("Sentry event not found (404) for issue %s — using webhook context only", issue_id)
        except Exception as exc:
            logger.warning("Could not fetch Sentry event details: %s", exc)

    # ── 2b. Read relevant source functions from repo ──────────────────────────
    # Merge frames from the API response with files listed in the webhook payload.
    import os as _os

    _CODE_ROOT = "/root/sentinel-workspace" if _os.path.isdir("/root/sentinel-workspace") else "/app"

    # Build a map of {filename: [lineno, ...]} from all known frames
    _frame_map: dict[str, list[int]] = {}
    for f in _sentry_frames:
        _frame_map.setdefault(f["filename"], []).append(f["lineno"])
    # Webhook-only files that had no API frame data — read without a target line
    for fname in issue_params.get("affected_files", []):
        if fname not in _frame_map:
            _frame_map[fname] = []

    # When the error originates entirely in a third-party library and no app/
    # frames were found (e.g. slack_sdk Socket Mode errors), grep our codebase
    # for files that import that library — those are the entry points where we
    # can add resilience improvements.
    if not _frame_map and _third_party_lib:
        try:
            grep_result = subprocess.run(
                [
                    "grep", "-rl",
                    _third_party_lib,
                    f"{_CODE_ROOT}/app",
                    "--include=*.py",
                ],
                capture_output=True, text=True, timeout=10,
            )
            for fpath in grep_result.stdout.strip().split("\n")[:4]:
                fpath = fpath.strip()
                if fpath:
                    rel = fpath.replace(f"{_CODE_ROOT}/", "")
                    if rel not in _frame_map:
                        _frame_map[rel] = []
            if _frame_map:
                logger.info(
                    "No app/ frames found; using %d caller file(s) for lib=%s",
                    len(_frame_map), _third_party_lib,
                )
        except Exception as exc:
            logger.warning("Could not grep for library callers (%s): %s", _third_party_lib, exc)

    file_context = ""
    for fname, linenos in _frame_map.items():
        fpath = f"{_CODE_ROOT}/{fname}"
        try:
            with open(fpath) as fh:
                all_lines = fh.readlines()
        except Exception as exc:
            logger.warning("Could not read source file %s: %s", fname, exc)
            continue

        if linenos:
            # Extract the function/class block containing each error line.
            # Walk upward from the error line to find the enclosing def/class,
            # then include from there to 60 lines past the error line.
            seen_ranges: list[tuple[int, int]] = []
            snippets: list[str] = []
            for lineno in sorted(set(linenos)):
                idx = lineno - 1  # 0-based
                # Walk up to find the enclosing def / class / async def
                fn_start = max(0, idx - 60)
                for i in range(idx, max(-1, idx - 120), -1):
                    stripped = all_lines[i].lstrip() if i < len(all_lines) else ""
                    if stripped.startswith(("def ", "async def ", "class ")):
                        fn_start = i
                        break
                fn_end = min(len(all_lines), lineno + 40)
                # Skip if this range already covered by a previous lineno
                if any(s <= fn_start and fn_end <= e for s, e in seen_ranges):
                    continue
                seen_ranges.append((fn_start, fn_end))
                block = []
                for i, line in enumerate(all_lines[fn_start:fn_end], fn_start + 1):
                    marker = "→ " if i in linenos else "  "
                    block.append(f"{i:4d} {marker}{line.rstrip()}")
                snippets.append("\n".join(block))
            excerpt = "\n\n".join(snippets)
        else:
            # No specific line — include first 3000 chars
            raw = "".join(all_lines)
            excerpt = raw[:3000] + ("\n... [truncated]" if len(raw) > 3000 else "")

        file_context += f"\n\n=== {fname} ===\n{excerpt}"
        logger.info("Read source file for LLM context | file=%s | lines=%s", fname, linenos)

    # ── 3. LLM fix plan ───────────────────────────────────────────────────────
    fix_plan: dict = {
        "fixable": False,
        "root_cause": "Analysis unavailable",
        "patches": [],
        "commit_message": f"fix: auto-fix for Sentry issue {issue_id}",
        "resolve_in_sentry": False,
        "summary": "LLM analysis could not be completed",
    }
    try:
        import anthropic

        context_block = f"\nStack trace:\n{issue_context}" if issue_context else ""
        file_block = file_context if file_context else ""
        available_files = list(_frame_map.keys())
        files_list = "\n".join(f"  - {f}" for f in available_files) if available_files else "  (none)"
        prompt = (
            f"You are an AI assistant that investigates and fixes software bugs reported in Sentry.\n\n"
            f"Issue: {title}\nLevel: {level}\nProject: {project}{context_block}{file_block}\n\n"
            f"Source files available for patching:\n{files_list}\n\n"
            "Analyze this error and decide if it can be fixed with a targeted code patch.\n"
            "Respond with ONLY a JSON object — no markdown, no explanation:\n"
            "{\n"
            '  "fixable": true/false,\n'
            '  "root_cause": "brief explanation",\n'
            '  "patches": [\n'
            '    {"file": "path/to/file.py", "old": "exact text to replace", "new": "replacement"}\n'
            "  ],\n"
            '  "commit_message": "fix: what was changed",\n'
            '  "resolve_in_sentry": true/false,\n'
            '  "summary": "human-readable summary of analysis and what was done"\n'
            "}\n\n"
            "Rules:\n"
            "- fixable=true for direct bug fixes AND for resilience improvements such as:\n"
            "    * adding retry / reconnection logic for transient network failures\n"
            "    * catching and suppressing expected exceptions from third-party libraries\n"
            "    * adding log filters to silence noisy SDK errors that are handled internally\n"
            "    * adding timeouts or backoff where missing\n"
            "- If the fix is ALREADY present in the source files above, set fixable=false,\n"
            "  resolve_in_sentry=true, and explain in summary that the fix is already deployed\n"
            "- fixable=false only when the root cause is purely environmental (expired credentials,\n"
            "  Slack API outage, DNS failure) AND no code improvement would help\n"
            "- The 'file' field in each patch MUST be one of the paths listed in 'Source files\n"
            "  available for patching' above — do not invent new file paths\n"
            "- Each 'old' must be an EXACT verbatim string copied from the file content above\n"
            "- resolve_in_sentry=true only if the patch fully addresses the root cause"
        )
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        # Extract the first complete JSON object — handles trailing text or truncation
        brace_depth = 0
        json_start = raw.find("{")
        json_end = -1
        if json_start != -1:
            for i, ch in enumerate(raw[json_start:], json_start):
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        json_end = i + 1
                        break
        if json_end != -1:
            raw = raw[json_start:json_end]
        fix_plan = json.loads(raw)
    except Exception as exc:
        logger.warning("LLM fix analysis failed: %s", exc)
        fix_plan["summary"] = f"LLM analysis failed: {exc}"

    # ── 4. Apply patches ──────────────────────────────────────────────────────
    patches_applied: list[str] = []
    patch_errors: list[str] = []
    pr_url: str = ""

    if fix_plan.get("fixable") and fix_plan.get("patches"):
        try:
            from app.integrations.repo import RepoClient

            repo = RepoClient()
            if repo.is_configured():
                await repo.ensure_repo()
                # Always work on a dedicated sentinel branch — never patch main directly
                import re as _re
                safe_id = _re.sub(r"[^a-zA-Z0-9\-]", "-", str(issue_id or task_id))[:40]
                branch = f"sentinel/sentry-{safe_id}"
                await repo.create_branch(branch)
                logger.info("Working on branch %s for Sentry issue %s", branch, issue_id)

                for patch in fix_plan["patches"]:
                    try:
                        await repo.patch_file(patch["file"], patch["old"], patch["new"])
                        patches_applied.append(patch["file"])
                        logger.info("Patched %s for Sentry issue %s", patch["file"], issue_id)
                    except Exception as exc:
                        patch_errors.append(f"{patch['file']}: {exc}")
                        logger.warning("Patch failed for %s: %s", patch["file"], exc)

                if patches_applied:
                    commit_msg = fix_plan.get("commit_message", f"fix: sentry issue {issue_id}")
                    await repo.commit(
                        f"{commit_msg}\n\nSentry issue: {issue_id}\nAuto-fixed by Sentinel",
                        files=patches_applied,
                    )
                    pr_result = await repo.push(
                        pr_title=f"fix(sentry): {title[:80]}",
                        pr_body=(
                            f"**Sentry issue:** {permalink or issue_id}\n"
                            f"**Level:** {level} | **Project:** {project}\n\n"
                            f"**Root cause:** {fix_plan.get('root_cause', 'See summary')}\n\n"
                            f"**Files changed:** {', '.join(f'`{f}`' for f in patches_applied)}\n\n"
                            f"_{fix_plan.get('summary', '')}_\n\n"
                            "---\n*Auto-generated by Sentinel. Review carefully before merging.*"
                        ),
                    )
                    # Extract URL from "Opened PR #N: https://..." return string
                    import re as _re2
                    m = _re2.search(r"(https://github\.com/\S+)", pr_result or "")
                    pr_url = m.group(1) if m else pr_result
            else:
                patch_errors.append("Repo not configured (GITHUB_BRAIN_REPO_URL not set)")
        except Exception as exc:
            patch_errors.append(f"Repo operation failed: {exc}")
            logger.error("Repo patch/commit/push failed: %s", exc, exc_info=True)

    # ── 5. Resolve in Sentry ──────────────────────────────────────────────────
    # Resolve when patches were applied, OR when the LLM confirmed the fix is
    # already deployed (fixable=False + resolve_in_sentry=True means "nothing
    # to patch, it's already fixed").
    _should_resolve = (
        fix_plan.get("resolve_in_sentry")
        and (patches_applied or not fix_plan.get("fixable"))
        and settings.sentry_auth_token
        and issue_id
    )
    if _should_resolve:
        try:
            import httpx

            async with httpx.AsyncClient() as http:
                await http.put(
                    f"https://sentry.io/api/0/issues/{issue_id}/",
                    headers={"Authorization": f"Bearer {settings.sentry_auth_token}"},
                    json={"status": "resolved"},
                    timeout=15,
                )
            logger.info("Resolved Sentry issue %s", issue_id)
        except Exception as exc:
            logger.warning("Could not resolve Sentry issue %s: %s", issue_id, exc)

    # ── 6. Slack summary ──────────────────────────────────────────────────────
    try:
        if patches_applied and not patch_errors and pr_url:
            status_line = f"✅ *Fix pushed — PR opened for your review*"
        elif patches_applied and not patch_errors:
            status_line = f"✅ *Fix pushed* — {len(patches_applied)} file(s) patched"
        elif patches_applied and patch_errors:
            status_line = f"⚠️ *Partially fixed* — {len(patches_applied)} patched, {len(patch_errors)} failed"
        elif fix_plan.get("fixable") and patch_errors:
            status_line = f"❌ *Fix failed* — {patch_errors[0][:120]}"
        elif not fix_plan.get("fixable") and fix_plan.get("resolve_in_sentry"):
            status_line = "✅ *Fix already deployed* — resolving in Sentry"
        else:
            status_line = "🔍 *Investigated* — not auto-fixable (environmental or complex)"

        sentry_link = f"<{permalink}|View in Sentry>" if permalink else f"Issue `{issue_id}`"
        lines = [
            f"{badge} *Sentry {level.upper()} — {project}* · task #{task_id}",
            f"*{title[:120]}*",
            sentry_link,
            "─" * 36,
            f"*Root cause:* {fix_plan.get('root_cause', 'Unknown')}",
            status_line,
        ]
        if patches_applied:
            lines.append(f"*Files changed:* {', '.join(f'`{f}`' for f in patches_applied)}")
        if pr_url:
            lines.append(f"*PR:* {pr_url}")
        if patch_errors:
            lines.append(f"*Patch errors:* {patch_errors[0][:120]}")
        if fix_plan.get("summary"):
            lines.append(f"_{fix_plan['summary'][:300]}_")

        await post_alert("\n".join(lines))
    except Exception as exc:
        logger.warning("Could not post Sentry fix Slack alert: %s", exc)

    # ── 7. Mark task done ─────────────────────────────────────────────────────
    final_status = "done" if not patch_errors or patches_applied else "failed"
    error_text = "; ".join(patch_errors[:3]) if patch_errors and not patches_applied else None
    _mark_task(task_id, final_status, error_text)

    return {
        "task_id": task_id,
        "fixable": fix_plan.get("fixable"),
        "patches_applied": patches_applied,
        "patch_errors": patch_errors,
        "status": final_status,
    }
