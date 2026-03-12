"""
GitHub Issue Ingestion & Triage Tasks

ingest_and_triage_github_issues — runs every 30 minutes.
  1. Fetch open issues for each enabled repo in github_repo_monitors.
  2. For each new issue, create a board task (approval_level=1 — auto-starts immediately).
  3. Skip issues already in the github_issues table.
  4. Dispatch investigate_and_fix_github_issue for each new task.
  5. Post a Slack run summary to sentinel-github.

Each investigate_and_fix_github_issue task posts its own Slack updates:
  - On start: "🧠 Investigating…"
  - On completion: "✅ Fix pushed — PR opened" or "💬 Commented on issue (not auto-fixable)"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import uuid

from celery import shared_task

logger = logging.getLogger(__name__)

_CODE_ROOT_CANDIDATES = ("/root/sentinel-workspace", "/app")


def _code_root() -> str:
    for p in _CODE_ROOT_CANDIDATES:
        if os.path.isdir(p):
            return p
    return "/app"


@shared_task(
    name="app.worker.github_tasks.ingest_and_triage_github_issues",
    bind=False,
    max_retries=1,
    default_retry_delay=120,
    soft_time_limit=300,
    time_limit=360,
)
def ingest_and_triage_github_issues(repo: str | None = None, limit: int | None = None) -> dict:
    """
    For each enabled monitor in github_repo_monitors, fetch open issues and
    create investigation tasks for any that haven't been seen before.
    """
    from app.db import postgres
    from app.config import get_settings
    from app.integrations.slack_notifier import post_alert_sync

    settings = get_settings()
    effective_limit = limit or settings.github_issue_poll_limit

    # ── 1. Fetch enabled monitors ─────────────────────────────────────────────
    try:
        where = "WHERE enabled = TRUE"
        params: tuple = ()
        if repo:
            where += " AND repo = %s"
            params = (repo,)
        monitors = postgres.execute(
            f"SELECT id, repo, agent_id, poll_labels, issue_filter FROM github_repo_monitors {where}",
            params or None,
        )
    except Exception as exc:
        logger.error("Failed to fetch github_repo_monitors: %s", exc)
        return {"error": str(exc)}

    if not monitors:
        logger.info("No enabled GitHub repo monitors found")
        return {"monitors": 0, "tasks_created": 0}

    total_created: list[dict] = []
    total_skipped = 0

    for monitor in monitors:
        monitor_id = monitor["id"]
        monitor_repo = monitor["repo"]
        agent_id = monitor.get("agent_id")
        poll_labels = monitor.get("poll_labels") or ""

        # ── 2. Fetch open issues for this repo ────────────────────────────────
        try:
            import httpx as _httpx

            headers = {
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            params_req: dict = {"state": "open", "per_page": effective_limit}
            if poll_labels:
                params_req["labels"] = poll_labels
            with _httpx.Client(timeout=15) as http:
                resp = http.get(
                    f"https://api.github.com/repos/{monitor_repo}/issues",
                    headers=headers,
                    params=params_req,
                )
            if resp.status_code == 404:
                logger.warning("GitHub repo not found or no access: %s", monitor_repo)
                continue
            resp.raise_for_status()
            raw_issues = resp.json()
        except Exception as exc:
            logger.error("Failed to fetch issues for %s: %s", monitor_repo, exc)
            continue

        # Update last_polled_at
        try:
            postgres.execute(
                "UPDATE github_repo_monitors SET last_polled_at = NOW(), updated_at = NOW() WHERE id = %s",
                (monitor_id,),
            )
        except Exception:
            pass

        for issue in raw_issues:
            # Skip PRs (GitHub returns them mixed with issues)
            if issue.get("pull_request"):
                continue

            issue_number = issue.get("number")
            issue_id = str(issue.get("id", ""))
            title = issue.get("title", "Unknown issue")
            state = issue.get("state", "open")
            labels_list = [lb.get("name", "") for lb in issue.get("labels", [])]
            labels_str = ",".join(labels_list)
            body_excerpt = (issue.get("body") or "")[:500]

            # ── 3. Deduplicate via github_issues table ─────────────────────────
            try:
                existing = postgres.execute_one(
                    "SELECT id, triage_status FROM github_issues WHERE repo = %s AND issue_number = %s",
                    (monitor_repo, issue_number),
                )
            except Exception as exc:
                logger.warning("Could not check existing github_issues row: %s", exc)
                existing = None

            if existing:
                total_skipped += 1
                continue

            # ── 4. INSERT into github_issues ───────────────────────────────────
            try:
                gi_row = postgres.execute_one(
                    """
                    INSERT INTO github_issues
                        (repo, issue_number, issue_id, title, state, labels, body_excerpt)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (repo, issue_number) DO NOTHING
                    RETURNING id
                    """,
                    (monitor_repo, issue_number, issue_id, title, state, labels_str, body_excerpt),
                )
            except Exception as exc:
                logger.error("Failed to insert github_issues row for %s#%s: %s", monitor_repo, issue_number, exc)
                continue

            if not gi_row:
                # Already exists (race — ON CONFLICT hit)
                total_skipped += 1
                continue

            gi_id = gi_row["id"]

            # ── 5. Create board task ────────────────────────────────────────────
            task_title = f"[GitHub] {monitor_repo}#{issue_number}: {title[:100]}"
            description = (
                f"GitHub repo: {monitor_repo}\n"
                f"Issue #: {issue_number}\n"
                f"Issue ID: {issue_id}\n"
                f"Labels: {labels_str}\n"
                f"URL: {issue.get('html_url', '')}\n\n"
                f"{body_excerpt}\n\n"
                "Auto-created by Sentinel github-triage. "
                "Investigating root cause and applying a fix if possible."
            )
            issue_params = {
                "repo": monitor_repo,
                "issue_number": issue_number,
                "issue_id": issue_id,
                "title": title,
                "labels": labels_list,
                "html_url": issue.get("html_url", ""),
                "body": issue.get("body") or "",
                "agent_id": str(agent_id) if agent_id else None,
                "gi_id": gi_id,
            }
            tags_list = ["github-triage", monitor_repo.replace("/", "-")]
            if labels_list:
                tags_list.extend(labels_list[:3])
            tags = json.dumps(tags_list)

            try:
                task_row = postgres.execute_one(
                    """
                    INSERT INTO tasks
                        (title, description, status, priority, priority_num,
                         approval_level, source, tags)
                    VALUES (%s, %s, 'pending', 'medium', 3, 1, 'github-triage', %s::jsonb)
                    RETURNING id
                    """,
                    (task_title, description, tags),
                )
            except Exception as exc:
                logger.error("Failed to insert task for %s#%s: %s", monitor_repo, issue_number, exc)
                continue

            task_id = task_row["id"]

            # Link github_issues row to task
            try:
                postgres.execute(
                    "UPDATE github_issues SET task_id = %s WHERE id = %s",
                    (task_id, gi_id),
                )
            except Exception:
                pass

            # ── 6. Dispatch investigation ──────────────────────────────────────
            try:
                investigate_and_fix_github_issue.apply_async(
                    args=[task_id, issue_params],
                    queue="tasks_general",
                )
                postgres.execute(
                    "UPDATE tasks SET celery_task_id = 'dispatched' WHERE id = %s",
                    (task_id,),
                )
                total_created.append({"task_id": task_id, "repo": monitor_repo, "issue_number": issue_number, "title": title})
                logger.info("Dispatched investigation for %s#%s → task #%s", monitor_repo, issue_number, task_id)
            except Exception as exc:
                logger.error("Failed to dispatch fix task for %s#%s: %s", monitor_repo, issue_number, exc)

    # ── 7. Slack run summary ──────────────────────────────────────────────────
    if total_created or total_skipped:
        try:
            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
            skip_note = f", {total_skipped} already tracked" if total_skipped else ""
            header = (
                f"🐙 *GitHub issue triage — {now_utc} — {len(monitors)} repo(s)*\n"
                f"Started {len(total_created)} investigation(s){skip_note}."
            )
            lines = [header, "─" * 36]
            for item in total_created[:10]:
                lines.append(f"🔵 task #{item['task_id']} · {item['repo']}#{item['issue_number']} · {item['title'][:70]}")
            post_alert_sync("\n".join(lines), settings.slack_github_channel)
        except Exception as exc:
            logger.warning("Could not post GitHub triage summary to Slack: %s", exc)

    return {
        "monitors_polled": len(monitors),
        "tasks_created": len(total_created),
        "tasks_skipped": total_skipped,
        "task_ids": [c["task_id"] for c in total_created],
    }


@shared_task(
    name="app.worker.github_tasks.investigate_and_fix_github_issue",
    bind=False,
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def investigate_and_fix_github_issue(task_id: int, issue_params: dict) -> dict:
    """Synchronous wrapper that calls the async investigation coroutine."""
    try:
        return asyncio.run(_investigate_and_fix_github(task_id, issue_params))
    except Exception as exc:
        logger.error("investigate_and_fix_github_issue task failed: %s", exc, exc_info=True)
        _mark_task_sync(task_id, "failed", str(exc)[:300])
        return {"error": str(exc), "task_id": task_id}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mark_task_sync(task_id: int, status: str, error: str | None = None) -> None:
    try:
        from app.db import postgres
        if error:
            postgres.execute(
                "UPDATE tasks SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, task_id),
            )
            logger.info("Task %s marked %s | error: %s", task_id, status, error[:500])
        else:
            postgres.execute(
                "UPDATE tasks SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, task_id),
            )
    except Exception as exc:
        logger.warning("Could not mark task %s as %s: %s", task_id, status, exc)


def _mark_github_issue(gi_id: int, triage_status: str, pr_url: str = "") -> None:
    try:
        from app.db import postgres
        postgres.execute(
            "UPDATE github_issues SET triage_status = %s, pr_url = %s, updated_at = NOW() WHERE id = %s",
            (triage_status, pr_url, gi_id),
        )
    except Exception as exc:
        logger.warning("Could not update github_issues row %s: %s", gi_id, exc)


async def _investigate_and_fix_github(task_id: int, issue_params: dict) -> dict:
    """
    1. Mark task in_progress + post "starting" Slack message.
    2. Fetch full issue body + comments from GitHub API.
    3. Extract file hints from issue text + read relevant source files.
    4. LLM (Haiku) → fix_plan JSON.
    5. If fixable:
       - If agent assigned: dispatch PATCH_INSTRUCTION via Redis.
       - Else: apply patch, commit branch, push, open PR.
    6. Comment on GitHub issue.
    7. Post Slack summary.
    8. Mark task done/failed.
    """
    from app.config import get_settings
    from app.integrations.slack_notifier import post_alert, post_alert_sync

    settings = get_settings()

    repo = issue_params.get("repo", "")
    issue_number = issue_params.get("issue_number", 0)
    issue_id = issue_params.get("issue_id", "")
    title = issue_params.get("title", "Unknown issue")
    html_url = issue_params.get("html_url", "")
    body = issue_params.get("body", "")
    labels = issue_params.get("labels", [])
    agent_id = issue_params.get("agent_id")
    gi_id = issue_params.get("gi_id")

    # ── 1. Mark in_progress + Slack start ─────────────────────────────────────
    _mark_task_sync(task_id, "in_progress")
    if gi_id:
        _mark_github_issue(gi_id, "investigating")
    try:
        issue_link = f"<{html_url}|{repo}#{issue_number}>" if html_url else f"`{repo}#{issue_number}`"
        post_alert_sync(
            f"🐙 *Investigating GitHub issue* — task #{task_id}\n"
            f"*{title[:120]}*\n"
            f"Repo: {repo} | Labels: {', '.join(labels) or 'none'} | Issue: {issue_link}",
            settings.slack_github_channel,
        )
    except Exception:
        pass

    # ── 2. Fetch full issue + comments from GitHub API ─────────────────────────
    gh_headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    issue_context = f"Title: {title}\nLabels: {', '.join(labels)}\n\nBody:\n{body}\n"

    try:
        import httpx

        async with httpx.AsyncClient(headers=gh_headers, timeout=15) as http:
            comments_resp = await http.get(
                f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
                params={"per_page": 20},
            )
            if comments_resp.status_code == 200:
                for c in comments_resp.json():
                    author = c.get("user", {}).get("login", "unknown")
                    comment_body = (c.get("body") or "")[:500]
                    issue_context += f"\n---\nComment by @{author}:\n{comment_body}\n"
    except Exception as exc:
        logger.warning("Could not fetch issue comments for %s#%s: %s", repo, issue_number, exc)

    # ── 3. Extract file hints + read relevant source files ─────────────────────
    # Look for file paths mentioned in issue body/comments
    _FILE_PATTERN = re.compile(
        r"\b(app/[a-zA-Z0-9_/]+\.py|[a-zA-Z0-9_/]+/[a-zA-Z0-9_/]+\.(?:py|js|ts|go|rs))\b"
    )
    mentioned_files = list(dict.fromkeys(_FILE_PATTERN.findall(issue_context)))[:5]

    code_root = _code_root()
    file_context = ""
    for fname in mentioned_files:
        fpath = os.path.join(code_root, fname)
        try:
            with open(fpath) as fh:
                raw = fh.read()
            excerpt = raw[:3000] + ("\n... [truncated]" if len(raw) > 3000 else "")
            file_context += f"\n\n=== {fname} ===\n{excerpt}"
            logger.info("Read source file for GitHub LLM context | file=%s", fname)
        except Exception:
            pass

    # If no files mentioned, try to grep for relevant terms from the title
    if not file_context and settings.github_token:
        try:
            search_term = re.sub(r"[^a-zA-Z0-9_]", " ", title).split()[:3]
            if search_term:
                grep_result = subprocess.run(
                    ["grep", "-rl", search_term[0], f"{code_root}/app", "--include=*.py"],
                    capture_output=True, text=True, timeout=10,
                )
                for fpath in grep_result.stdout.strip().split("\n")[:3]:
                    fpath = fpath.strip()
                    if fpath:
                        rel = fpath.replace(f"{code_root}/", "")
                        try:
                            with open(fpath) as fh:
                                raw = fh.read()
                            file_context += f"\n\n=== {rel} ===\n{raw[:2000]}"
                        except Exception:
                            pass
        except Exception:
            pass

    # ── 4. LLM fix plan ───────────────────────────────────────────────────────
    fix_plan: dict = {
        "fixable": False,
        "root_cause": "Analysis unavailable",
        "patches": [],
        "commit_message": f"fix: address GitHub issue #{issue_number} in {repo}",
        "summary": "LLM analysis could not be completed",
    }
    try:
        import anthropic

        available_files = mentioned_files if mentioned_files else ["(no specific files mentioned)"]
        files_list = "\n".join(f"  - {f}" for f in available_files)
        prompt = (
            f"You are an AI assistant that investigates and fixes bugs reported in GitHub issues.\n\n"
            f"Repository: {repo}\n"
            f"Issue #{issue_number}: {title}\n"
            f"Labels: {', '.join(labels) or 'none'}\n\n"
            f"Issue content:\n{issue_context[:3000]}\n\n"
            f"{('Source files:\\n' + file_context[:4000]) if file_context else ''}\n\n"
            f"Files available for patching:\n{files_list}\n\n"
            "Analyze this GitHub issue and decide if it can be fixed with a targeted code patch.\n"
            "Respond with ONLY a JSON object — no markdown, no explanation:\n"
            "{\n"
            '  "fixable": true/false,\n'
            '  "root_cause": "brief explanation",\n'
            '  "patches": [\n'
            '    {"file": "path/to/file.py", "old": "exact text to replace", "new": "replacement"}\n'
            "  ],\n"
            '  "commit_message": "fix: what was changed",\n'
            '  "summary": "human-readable summary of analysis and what was done"\n'
            "}\n\n"
            "Rules:\n"
            "- fixable=true only when you have source files above AND can produce an exact text patch\n"
            "- fixable=false when the issue is a feature request, unclear, or needs human design\n"
            "- Each 'old' must be an EXACT verbatim string from the source file above\n"
            "- The 'file' must be one of the paths in 'Files available for patching'"
        )
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
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
        logger.warning("LLM fix analysis failed for %s#%s: %s", repo, issue_number, exc)
        fix_plan["summary"] = f"LLM analysis failed: {exc}"

    # ── 5. Apply patches ──────────────────────────────────────────────────────
    patches_applied: list[str] = []
    patch_errors: list[str] = []
    pr_url: str = ""

    if fix_plan.get("fixable") and fix_plan.get("patches"):
        if agent_id:
            # ── 5a. Dispatch to mesh agent via Redis ───────────────────────────
            try:
                import redis as _redis

                # Build a unified diff string from patches
                diff_lines: list[str] = []
                for p in fix_plan["patches"]:
                    diff_lines.append(f"--- a/{p['file']}")
                    diff_lines.append(f"+++ b/{p['file']}")
                    for line in (p.get("old") or "").splitlines():
                        diff_lines.append(f"-{line}")
                    for line in (p.get("new") or "").splitlines():
                        diff_lines.append(f"+{line}")
                unified_diff = "\n".join(diff_lines)

                patch_msg = {
                    "type": "PATCH_INSTRUCTION",
                    "agent_id": agent_id,
                    "ts": int(time.time()),
                    "payload": {
                        "patch_id": str(uuid.uuid4()),
                        "triggered_by": "github",
                        "diff_text": unified_diff,
                        "files_changed": [p["file"] for p in fix_plan["patches"]],
                        "commit_message": fix_plan.get("commit_message", f"fix: GitHub issue #{issue_number}"),
                        "restart_app": True,
                    },
                }
                rc = _redis.Redis(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    password=settings.redis_password,
                    decode_responses=True,
                )
                rc.lpush(f"sentinel:agent:cmd:{agent_id}", json.dumps(patch_msg))
                patches_applied = [p["file"] for p in fix_plan["patches"]]
                pr_url = f"dispatched-to-agent:{agent_id}"
                logger.info("Dispatched PATCH_INSTRUCTION to agent %s for %s#%s", agent_id, repo, issue_number)
            except Exception as exc:
                patch_errors.append(f"Agent dispatch failed: {exc}")
                logger.error("Agent patch dispatch failed: %s", exc)
        else:
            # ── 5b. Apply locally, commit, push, open PR ───────────────────────
            try:
                from app.integrations.repo import RepoClient

                repo_client = RepoClient()
                if repo_client.is_configured():
                    await repo_client.ensure_repo()
                    repo_slug = re.sub(r"[^a-zA-Z0-9\-]", "-", repo)[:30]
                    branch = f"sentinel/github-{repo_slug}-{issue_number}"
                    await repo_client.create_branch(branch)

                    for patch in fix_plan["patches"]:
                        try:
                            await repo_client.patch_file(patch["file"], patch["old"], patch["new"])
                            patches_applied.append(patch["file"])
                        except Exception as exc:
                            patch_errors.append(f"{patch['file']}: {exc}")
                            logger.warning("Patch failed for %s: %s", patch["file"], exc)

                    if patches_applied:
                        commit_msg = fix_plan.get("commit_message", f"fix: GitHub issue #{issue_number}")
                        await repo_client.commit(
                            f"{commit_msg}\n\nGitHub issue: {html_url}\nAuto-fixed by Sentinel",
                            files=patches_applied,
                        )
                        pr_result = await repo_client.push(
                            pr_title=f"fix(github): {title[:80]}",
                            pr_body=(
                                f"**GitHub issue:** {html_url}\n"
                                f"**Repo:** {repo} | **Issue:** #{issue_number}\n\n"
                                f"**Root cause:** {fix_plan.get('root_cause', 'See summary')}\n\n"
                                f"**Files changed:** {', '.join(f'`{f}`' for f in patches_applied)}\n\n"
                                f"_{fix_plan.get('summary', '')}_\n\n"
                                f"Closes #{issue_number}\n\n"
                                "---\n*Auto-generated by Sentinel. Review carefully before merging.*"
                            ),
                        )
                        m = re.search(r"(https://github\.com/\S+)", pr_result or "")
                        pr_url = m.group(1) if m else pr_result
                else:
                    patch_errors.append("Repo not configured (GITHUB_BRAIN_REPO_URL not set)")
            except Exception as exc:
                patch_errors.append(f"Repo operation failed: {exc}")
                logger.error("Repo patch/commit/push failed for GitHub issue: %s", exc, exc_info=True)

    # ── 6. Comment on GitHub issue ─────────────────────────────────────────────
    comment_posted = False
    try:
        import httpx

        root_cause = fix_plan.get("root_cause", "Under investigation")
        summary = fix_plan.get("summary", "")
        if patches_applied and pr_url and not pr_url.startswith("dispatched-to-agent"):
            comment_body = (
                f"🤖 **Sentinel investigated this issue.**\n\n"
                f"**Root cause:** {root_cause}\n\n"
                f"**Auto-fix PR:** {pr_url}\n\n"
                f"_{summary[:400]}_\n\n"
                f"*Reviewed and merged the PR to close this issue.*"
            )
        elif pr_url.startswith("dispatched-to-agent:"):
            comment_body = (
                f"🤖 **Sentinel dispatched a patch to the responsible agent.**\n\n"
                f"**Root cause:** {root_cause}\n\n"
                f"**Files:** {', '.join(patches_applied)}\n\n"
                f"_{summary[:400]}_"
            )
        else:
            comment_body = (
                f"🤖 **Sentinel investigated this issue.**\n\n"
                f"**Root cause:** {root_cause}\n\n"
                f"_{summary[:400]}_\n\n"
                f"*This issue requires manual review — no automatic fix was possible.*"
            )

        async with httpx.AsyncClient(headers=gh_headers, timeout=15) as http:
            cr = await http.post(
                f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
                json={"body": comment_body},
            )
            if cr.status_code in (200, 201):
                comment_posted = True
    except Exception as exc:
        logger.warning("Could not post GitHub issue comment: %s", exc)

    # ── 7. Update github_issues row ────────────────────────────────────────────
    if gi_id:
        final_gi_status = (
            "patched" if patches_applied and not patch_errors
            else "commented" if comment_posted
            else "failed"
        )
        _mark_github_issue(gi_id, final_gi_status, pr_url)

    # ── 8. Slack summary ───────────────────────────────────────────────────────
    try:
        if patches_applied and not patch_errors and pr_url and not pr_url.startswith("dispatched"):
            status_line = "✅ *Fix pushed — PR opened for your review*"
        elif patches_applied and pr_url.startswith("dispatched-to-agent:"):
            status_line = f"✅ *Patch dispatched to agent* `{agent_id[:8]}...`"
        elif patches_applied and patch_errors:
            status_line = f"⚠️ *Partially fixed* — {len(patches_applied)} patched, {len(patch_errors)} failed"
        elif fix_plan.get("fixable") and patch_errors:
            status_line = f"❌ *Fix failed* — {patch_errors[0][:120]}"
        else:
            status_line = "🔍 *Investigated* — not auto-fixable, commented on issue"

        issue_link = f"<{html_url}|{repo}#{issue_number}>" if html_url else f"`{repo}#{issue_number}`"
        lines = [
            f"🐙 *GitHub Issue — {repo}* · task #{task_id}",
            f"*#{issue_number}: {title[:100]}*",
            issue_link,
            "─" * 36,
            f"*Root cause:* {fix_plan.get('root_cause', 'Unknown')}",
            status_line,
        ]
        if patches_applied and not pr_url.startswith("dispatched"):
            lines.append(f"*Files changed:* {', '.join(f'`{f}`' for f in patches_applied)}")
        if pr_url and not pr_url.startswith("dispatched"):
            lines.append(f"*PR:* {pr_url}")
        if patch_errors:
            lines.append(f"*Patch errors:* {patch_errors[0][:120]}")
        if fix_plan.get("summary"):
            lines.append(f"_{fix_plan['summary'][:300]}_")
        if comment_posted:
            lines.append(f"_💬 Commented on GitHub issue_")

        await post_alert("\n".join(lines), settings.slack_github_channel)
    except Exception as exc:
        logger.warning("Could not post GitHub issue Slack summary: %s", exc)

    # ── 9. Mark task done ──────────────────────────────────────────────────────
    final_status = "done" if not patch_errors or patches_applied else "failed"
    error_text = "; ".join(patch_errors[:3]) if patch_errors and not patches_applied else None
    _mark_task_sync(task_id, final_status, error_text)

    return {
        "task_id": task_id,
        "repo": repo,
        "issue_number": issue_number,
        "fixable": fix_plan.get("fixable"),
        "patches_applied": patches_applied,
        "patch_errors": patch_errors,
        "pr_url": pr_url,
        "comment_posted": comment_posted,
        "status": final_status,
    }
