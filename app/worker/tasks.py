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
    default_retry_delay=300,   # 5 min between retries
    soft_time_limit=3_600,     # 1hr soft limit — logs warning
    time_limit=3_900,          # 1hr 5min hard limit — kills worker
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
    max_retries=0,          # no retries — next run is in 30 min anyway
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
    max_retries=0,          # don't retry — a failed deploy needs human eyes
    soft_time_limit=360,    # 6 min soft limit
    time_limit=420,         # 7 min hard kill
)
def deploy_brain(self, reason: str = "") -> dict:
    """
    Pull latest code from GitHub → rebuild Brain Docker image → restart container.
    Runs inside the Celery worker, which has /var/run/docker.sock and /sentinel-project mounted.
    """
    try:
        return asyncio.run(_deploy_brain(reason))
    except Exception as exc:
        logger.error("deploy_brain task failed: %s", exc, exc_info=True)
        post_alert_sync(f"❌ *Brain deploy FAILED*\n`{type(exc).__name__}: {exc}`")
        return {"success": False, "error": str(exc)}


@celery_app.task(
    bind=True,
    name="app.worker.tasks.investigate_and_fix_sentry_issue",
    queue="celery",
    max_retries=1,
    default_retry_delay=60,
    soft_time_limit=300,   # 5 min soft limit
    time_limit=360,        # 6 min hard kill
)
def investigate_and_fix_sentry_issue(self, task_id: str, issue_params: dict) -> dict:
    """Auto-investigate a Sentry issue and attempt a code fix via LLM + RepoClient."""
    try:
        return asyncio.run(_investigate_and_fix(task_id, issue_params))
    except Exception as exc:
        logger.error("investigate_and_fix_sentry_issue failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


# ── Async implementations ──────────────────────────────────────────────────────

async def _weekly_evals() -> dict:
    from app.evals.runner   import EvalRunner
    from app.evals.reporter import post_scorecard_to_slack

    runner    = EvalRunner()
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
    from app.evals.reporter     import post_integration_health_to_slack

    results = await run_all_integration_evals()
    passed  = sum(1 for r in results if r.passed)
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
            resp   = await http.get("http://brain:8000/api/v1/health", timeout=10)
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


def post_alert_sync(text: str) -> None:
    """Fire-and-forget sync Slack alert (used inside Celery tasks)."""
    from app.integrations.slack_notifier import post_alert_sync as _pas
    _pas(text)


async def _deploy_brain(reason: str) -> dict:
    """
    1. Post 'deploy started' to Slack.
    2. Sleep 5 s — gives the brain time to finish sending its response.
    3. git pull origin main  (updates /sentinel-project = /root/sentinel on host).
    4. docker compose build brain.
    5. docker compose up -d brain  (hot-swap with new image).
    6. Post result to Slack.
    """
    from app.integrations.slack_notifier import post_alert_sync as _notify

    project_dir  = "/sentinel-project"
    compose_file = f"{project_dir}/docker-compose.yml"
    steps:  list[str] = []
    errors: list[str] = []

    _notify(
        f"🔄 *Brain deploy started*\n"
        f"Reason: _{reason or 'manual request'}_\n"
        "_Pulling latest code and rebuilding image..._"
    )

    await asyncio.sleep(5)   # let the brain finish its HTTP/Slack response

    # ── 1. git pull ───────────────────────────────────────────────────────────
    try:
        env = {**os.environ, "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=no"}
        r = subprocess.run(
            ["git", "-C", project_dir, "pull", "origin", "main"],
            capture_output=True, text=True, timeout=60, env=env,
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
            ["docker", "compose", "-p", "sentinel",
             "-f", compose_file, "build", "brain"],
            capture_output=True, text=True, timeout=300,
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
            ["docker", "compose", "-p", "sentinel",
             "-f", compose_file, "up", "-d", "--no-deps", "brain"],
            capture_output=True, text=True, timeout=60,
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


async def _investigate_and_fix(task_id: str, issue_params: dict) -> dict:
    """
    1. Mark task as executing in DB.
    2. Fetch stack-trace context from Sentry API (if auth token configured).
    3. Ask LLM (Haiku) for a structured fix plan as JSON.
    4. Apply file patches via RepoClient → commit → push (if fixable and repo configured).
    5. Resolve in Sentry (if LLM says it's fully addressed).
    6. Post Slack summary to brain-alerts.
    7. Mark task completed / failed.
    """
    from app.db import postgres
    from app.config import get_settings
    from app.integrations.slack_notifier import post_alert

    settings = get_settings()

    issue_id   = issue_params.get("issue_id", "")
    title      = issue_params.get("title", "Unknown error")
    level      = issue_params.get("level", "error")
    project    = issue_params.get("project", "")
    permalink  = issue_params.get("permalink", "")

    # ── 1. Mark executing ──────────────────────────────────────────────────────
    try:
        postgres.execute(
            "UPDATE pending_write_tasks SET status='executing', updated_at=NOW() WHERE task_id=%s",
            (task_id,),
        )
    except Exception as exc:
        logger.warning("Could not mark task executing: %s", exc)

    # ── 2. Fetch Sentry stack trace ────────────────────────────────────────────
    issue_context = ""
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
                for entry in event.get("entries", []):
                    if entry.get("type") != "exception":
                        continue
                    for exc_val in (entry.get("data") or {}).get("values", [])[:2]:
                        exc_type  = exc_val.get("type", "")
                        exc_value = exc_val.get("value", "")
                        issue_context += f"Exception: {exc_type}: {exc_value}\n"
                        frames = (exc_val.get("stacktrace") or {}).get("frames", [])
                        for frame in frames[-6:]:
                            fname  = frame.get("filename", "")
                            lineno = frame.get("lineno", "")
                            func   = frame.get("function", "")
                            ctx    = (frame.get("context_line") or "").strip()
                            issue_context += f"  File {fname}:{lineno} in {func}\n"
                            if ctx:
                                issue_context += f"    {ctx}\n"
        except Exception as exc:
            logger.warning("Could not fetch Sentry event details: %s", exc)

    # ── 2b. Read affected source files from repo ──────────────────────────────
    import os as _os
    _CODE_ROOT = "/sentinel-project" if _os.path.isdir("/sentinel-project") else "/app"
    file_context = ""
    for fname in issue_params.get("affected_files", []):
        try:
            content = open(f"{_CODE_ROOT}/{fname}").read()
            file_context += f"\n\n=== {fname} ===\n{content}"
            logger.info("Read source file for LLM context | file={}", fname)
        except Exception as exc:
            logger.warning("Could not read source file {}: {}", fname, exc)

    # ── 3. LLM fix plan ───────────────────────────────────────────────────────
    fix_plan: dict = {
        "fixable":          False,
        "root_cause":       "Analysis unavailable",
        "patches":          [],
        "commit_message":   f"fix: auto-fix for Sentry issue {issue_id}",
        "resolve_in_sentry": False,
        "summary":          "LLM analysis could not be completed",
    }
    try:
        import anthropic
        context_block = f"\nStack trace:\n{issue_context}" if issue_context else ""
        file_block    = file_context if file_context else ""
        prompt = (
            f"You are an AI assistant that investigates and fixes software bugs reported in Sentry.\n\n"
            f"Issue: {title}\nLevel: {level}\nProject: {project}{context_block}{file_block}\n\n"
            "Analyze this error and decide if it can be fixed with a targeted, surgical code patch.\n"
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
            "- fixable=true only when you have HIGH confidence the patch will resolve the issue\n"
            "- Each 'old' must be an EXACT verbatim string copied from the file content above\n"
            "- For environmental issues (network, config, credentials): fixable=false\n"
            "- resolve_in_sentry=true only if the patch fully addresses the root cause"
        )
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if the model wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        fix_plan = json.loads(raw)
    except Exception as exc:
        logger.warning("LLM fix analysis failed: %s", exc)
        fix_plan["summary"] = f"LLM analysis failed: {exc}"

    # ── 4. Apply patches ──────────────────────────────────────────────────────
    patches_applied: list[str] = []
    patch_errors:    list[str] = []

    if fix_plan.get("fixable") and fix_plan.get("patches"):
        try:
            from app.integrations.repo import RepoClient
            repo = RepoClient()
            if repo.is_configured():
                await repo.ensure_repo()
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
                    await repo.commit(f"{commit_msg}\n\nSentry issue: {issue_id}\nAuto-fixed by Brain")
                    await repo.push()
            else:
                patch_errors.append("Repo not configured (GITHUB_BRAIN_REPO_URL not set)")
        except Exception as exc:
            patch_errors.append(f"Repo operation failed: {exc}")
            logger.error("Repo patch/commit/push failed: %s", exc, exc_info=True)

    # ── 5. Resolve in Sentry ──────────────────────────────────────────────────
    if fix_plan.get("resolve_in_sentry") and patches_applied and settings.sentry_auth_token and issue_id:
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
        badge = "🔴" if level in ("fatal", "critical") else "🟠" if level == "error" else "🟡"
        if patches_applied and not patch_errors:
            status_line = f"✅ *Auto-fixed* — {len(patches_applied)} file(s) patched & pushed"
        elif patches_applied and patch_errors:
            status_line = f"⚠️ *Partially fixed* — {len(patches_applied)} patched, {len(patch_errors)} failed"
        elif fix_plan.get("fixable") and patch_errors:
            status_line = f"❌ *Fix failed* — {patch_errors[0]}"
        else:
            status_line = "🔍 *Investigated* — not auto-fixable (environmental or complex)"

        link_text = f"<{permalink}|View in Sentry>" if permalink else f"Issue `{issue_id}`"
        lines = [
            f"{badge} *Sentry {level.upper()} — {project}*",
            f"*{title[:120]}*",
            link_text,
            "─" * 36,
            f"*Root cause:* {fix_plan.get('root_cause', 'Unknown')}",
            status_line,
        ]
        if patches_applied:
            lines.append(f"*Files:* {', '.join(f'`{f}`' for f in patches_applied)}")
        if patch_errors:
            lines.append(f"*Errors:* {patch_errors[0]}")
        if fix_plan.get("summary"):
            lines.append(f"_{fix_plan['summary']}_")

        await post_alert("\n".join(lines))
    except Exception as exc:
        logger.warning("Could not post Sentry fix Slack alert: %s", exc)

    # ── 7. Mark task done ─────────────────────────────────────────────────────
    final_status = "completed" if not patch_errors or patches_applied else "failed"
    error_text   = "; ".join(patch_errors[:3]) if patch_errors and not patches_applied else None
    try:
        postgres.execute(
            "UPDATE pending_write_tasks SET status=%s, error=%s, updated_at=NOW() WHERE task_id=%s",
            (final_status, error_text, task_id),
        )
    except Exception as exc:
        logger.warning("Could not update task to %s: %s", final_status, exc)

    return {
        "task_id":         task_id,
        "fixable":         fix_plan.get("fixable"),
        "patches_applied": patches_applied,
        "patch_errors":    patch_errors,
        "status":          final_status,
    }
