"""
PR Review & Auto-Merge Tasks

When Sentinel opens a PR (or GitHub fires a webhook for any PR), this module:
  1. Checks for merge conflicts.
  2. If conflicts exist: fetches conflicting files, uses LLM to resolve them,
     commits the resolution to the PR branch, and pushes.
  3. Waits for CI status checks to pass (up to ~10 min).
  4. If everything is green: approves and squash-merges the PR.
  5. If conflicts cannot be resolved or CI fails: DMs the owner with full details.

Entry points:
  review_and_merge_pr(pr_number)   — Celery task, called by webhook or beat poll
  poll_open_sentinel_prs()         — Beat task, catches any PRs that missed the webhook
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from app.worker.celery_app import celery_app
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_GH_API = "https://api.github.com"
_SENTINEL_BRANCH_RE = re.compile(r"^sentinel/")


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(path: str, params: dict | None = None) -> Any:
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{_GH_API}{path}", headers=_gh_headers(), params=params)
        r.raise_for_status()
        return r.json()


def _gh_post(path: str, payload: dict) -> Any:
    with httpx.Client(timeout=20) as c:
        r = c.post(f"{_GH_API}{path}", headers=_gh_headers(), json=payload)
        r.raise_for_status()
        return r.json()


def _gh_put(path: str, payload: dict) -> Any:
    with httpx.Client(timeout=20) as c:
        r = c.put(f"{_GH_API}{path}", headers=_gh_headers(), json=payload)
        r.raise_for_status()
        return r.json()


# ── LLM conflict resolver ─────────────────────────────────────────────────────

def _resolve_conflict_with_llm(filename: str, conflicted_content: str) -> str | None:
    """
    Ask Claude Haiku to resolve git conflict markers in *conflicted_content*.
    Returns the resolved file content, or None if it cannot determine the right merge.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = (
        f"You are resolving a git merge conflict in `{filename}`.\n\n"
        "The file contains standard git conflict markers:\n"
        "  <<<<<<< HEAD\n  ... current branch code ...\n  =======\n"
        "  ... incoming branch code ...\n  >>>>>>> branch-name\n\n"
        "Rules:\n"
        "- Produce the correct merged file with ALL conflict markers removed.\n"
        "- Preserve all non-conflicting code exactly as-is.\n"
        "- When both sides add different things, keep BOTH (additive merge).\n"
        "- When the changes are semantically incompatible and you cannot safely "
        "determine the correct resolution, respond with exactly: CANNOT_RESOLVE\n\n"
        f"File content:\n```\n{conflicted_content[:12000]}\n```\n\n"
        "Return ONLY the resolved file content (no markdown fences, no explanation), "
        "or CANNOT_RESOLVE."
    )
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    result = msg.content[0].text.strip()
    if result == "CANNOT_RESOLVE" or "CANNOT_RESOLVE" in result[:30]:
        return None
    return result


# ── Core review logic ──────────────────────────────────────────────────────────

def _review_pr_sync(pr_number: int) -> dict:
    from app.integrations.slack_notifier import post_dm_sync, post_alert_sync
    from app.integrations.repo import _resolve_workspace, _run

    repo = settings.github_default_repo
    if not repo or not settings.github_token:
        return {"skipped": "GitHub not configured"}

    # ── 1. Fetch PR details ────────────────────────────────────────────────────
    try:
        pr = _gh_get(f"/repos/{repo}/pulls/{pr_number}")
    except Exception as exc:
        logger.error("Could not fetch PR #%d: %s", pr_number, exc)
        return {"error": str(exc)}

    branch = pr["head"]["ref"]
    pr_title = pr["title"]
    pr_url = pr["html_url"]
    pr_state = pr["state"]

    if pr_state != "open":
        return {"skipped": f"PR #{pr_number} is {pr_state}"}

    logger.info("Reviewing PR #%d (%s) branch=%s", pr_number, pr_title, branch)

    # ── 2. Wait for GitHub's mergeability check (can be null briefly after open) ─
    mergeable = pr.get("mergeable")
    for _ in range(6):
        if mergeable is not None:
            break
        time.sleep(5)
        pr = _gh_get(f"/repos/{repo}/pulls/{pr_number}")
        mergeable = pr.get("mergeable")

    workspace = _resolve_workspace()

    # ── 3. Resolve conflicts if needed ────────────────────────────────────────
    unresolvable: list[str] = []
    if mergeable is False:
        logger.info("PR #%d has merge conflicts — attempting LLM resolution", pr_number)

        try:
            # Checkout the PR branch and attempt merge to surface conflict markers
            _run(["git", "fetch", "origin"], workspace)
            _run(["git", "checkout", branch], workspace)
            _run(["git", "config", "user.email", "brain@csuitecode.com"], workspace)
            _run(["git", "config", "user.name", "AI Brain"], workspace)
            merge_out = _run(
                ["git", "merge", "origin/main", "--no-commit", "--no-ff"],
                workspace,
                check=False,
            )
        except Exception as exc:
            logger.error("git merge failed for PR #%d: %s", pr_number, exc)
            unresolvable.append(f"git merge error: {exc}")
            merge_out = ""

        if not unresolvable:
            # Find conflicted files
            status_out = _run(["git", "status", "--short"], workspace)
            conflicted_files = [
                line[3:].strip()
                for line in status_out.splitlines()
                if line.startswith("UU") or line.startswith("AA") or line.startswith("DD")
            ]

            if not conflicted_files:
                # Merge may have auto-resolved
                logger.info("No conflict markers found — merge may have auto-resolved")
            else:
                for cf in conflicted_files:
                    file_path = workspace / cf
                    try:
                        content = file_path.read_text(errors="replace")
                    except Exception:
                        unresolvable.append(cf)
                        continue

                    resolved = _resolve_conflict_with_llm(cf, content)
                    if resolved is None:
                        unresolvable.append(cf)
                        logger.warning("LLM could not resolve conflict in %s", cf)
                    else:
                        file_path.write_text(resolved)
                        _run(["git", "add", "--", cf], workspace)
                        logger.info("Resolved conflict in %s", cf)

            if unresolvable:
                # Abort the merge attempt before notifying
                _run(["git", "merge", "--abort"], workspace, check=False)
            else:
                # Commit and push the resolution
                try:
                    if conflicted_files:
                        _run(
                            ["git", "commit", "-m", f"fix: resolve merge conflicts for PR #{pr_number}"],
                            workspace,
                        )
                    else:
                        _run(["git", "commit", "--no-edit"], workspace, check=False)
                    _run(["git", "push", "origin", branch], workspace)
                    logger.info("Pushed conflict resolution for PR #%d", pr_number)
                except Exception as exc:
                    logger.error("Failed to push resolution for PR #%d: %s", pr_number, exc)
                    unresolvable.append(f"push failed: {exc}")

    # ── 4. Notify owner and bail if conflicts are unresolvable ─────────────────
    if unresolvable:
        msg = (
            f"⚠️ *PR #{pr_number} has unresolvable merge conflicts*\n"
            f"Title: {pr_title}\n"
            f"Branch: `{branch}`\n"
            f"Link: {pr_url}\n\n"
            f"Files I could not resolve:\n"
            + "\n".join(f"  • `{f}`" for f in unresolvable)
            + "\n\n_Manual resolution required before this can be merged._"
        )
        post_dm_sync(msg)
        post_alert_sync(msg)
        return {"pr_number": pr_number, "status": "needs_manual_resolution", "files": unresolvable}

    # ── 5. Wait for CI status checks ──────────────────────────────────────────
    head_sha = pr["head"]["sha"]
    ci_result = _wait_for_ci(repo, head_sha, timeout_seconds=600)

    if ci_result["conclusion"] == "failure":
        msg = (
            f"❌ *PR #{pr_number} — CI checks failed*\n"
            f"Title: {pr_title}\n"
            f"Branch: `{branch}`\n"
            f"Link: {pr_url}\n\n"
            f"Failed checks:\n"
            + "\n".join(f"  • `{c}`" for c in ci_result.get("failed_checks", []))
            + "\n\n_Fix the failures before merging._"
        )
        post_dm_sync(msg)
        post_alert_sync(msg)
        return {"pr_number": pr_number, "status": "ci_failed", "checks": ci_result}

    if ci_result["conclusion"] == "timeout":
        msg = (
            f"⏱ *PR #{pr_number} — CI timed out waiting for checks*\n"
            f"Title: {pr_title}\n"
            f"Link: {pr_url}\n\n"
            "_Checks are still running. Please review and merge manually._"
        )
        post_dm_sync(msg)
        return {"pr_number": pr_number, "status": "ci_timeout"}

    # ── 6. Approve the PR ─────────────────────────────────────────────────────
    try:
        _gh_post(
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            {"event": "APPROVE", "body": "Auto-approved by Sentinel: no conflicts, CI green."},
        )
        logger.info("Approved PR #%d", pr_number)
    except Exception as exc:
        logger.warning("Could not approve PR #%d: %s", pr_number, exc)

    # ── 7. Squash-merge ───────────────────────────────────────────────────────
    try:
        _gh_put(
            f"/repos/{repo}/pulls/{pr_number}/merge",
            {
                "commit_title": f"{pr_title} (#{pr_number})",
                "commit_message": "Squash-merged by Sentinel after CI passed.",
                "merge_method": "squash",
            },
        )
        logger.info("Merged PR #%d", pr_number)
    except Exception as exc:
        # Branch protection may require human merge — that's fine
        logger.warning("Could not auto-merge PR #%d: %s — notifying owner", pr_number, exc)
        msg = (
            f"✅ *PR #{pr_number} approved — merge requires your action*\n"
            f"Title: {pr_title}\n"
            f"Link: {pr_url}\n\n"
            f"CI passed and conflicts resolved. Branch protection blocked auto-merge: `{exc}`"
        )
        post_dm_sync(msg)
        post_alert_sync(msg)
        return {"pr_number": pr_number, "status": "approved_needs_manual_merge"}

    # ── 8. Success notification ───────────────────────────────────────────────
    msg = (
        f"✅ *PR #{pr_number} merged*\n"
        f"Title: {pr_title}\n"
        f"Branch: `{branch}`\n"
        f"Link: {pr_url}\n\n"
        "_No conflicts. CI passed. Squash-merged by Sentinel._"
    )
    post_alert_sync(msg)
    return {"pr_number": pr_number, "status": "merged"}


def _wait_for_ci(repo: str, sha: str, timeout_seconds: int = 600) -> dict:
    """
    Poll GitHub check runs for *sha* until all complete or timeout.
    Returns {"conclusion": "success"|"failure"|"timeout", "failed_checks": [...]}
    """
    deadline = time.time() + timeout_seconds
    poll_interval = 20

    while time.time() < deadline:
        try:
            data = _gh_get(f"/repos/{repo}/commits/{sha}/check-runs")
        except Exception as exc:
            logger.warning("Could not fetch check runs: %s", exc)
            time.sleep(poll_interval)
            continue

        runs = data.get("check_runs", [])
        if not runs:
            # No checks configured — treat as pass
            return {"conclusion": "success", "failed_checks": []}

        in_progress = [r for r in runs if r["status"] in ("queued", "in_progress")]
        if in_progress:
            logger.debug("CI in progress (%d checks pending) for %s", len(in_progress), sha[:8])
            time.sleep(poll_interval)
            continue

        failed = [
            r["name"] for r in runs
            if r["conclusion"] not in ("success", "neutral", "skipped", None)
        ]
        if failed:
            return {"conclusion": "failure", "failed_checks": failed}
        return {"conclusion": "success", "failed_checks": []}

    return {"conclusion": "timeout", "failed_checks": []}


def _list_open_sentinel_prs() -> list[int]:
    """Return PR numbers for all open sentinel/* PRs in the configured repo."""
    repo = settings.github_default_repo
    if not repo or not settings.github_token:
        return []
    try:
        prs = _gh_get(f"/repos/{repo}/pulls", params={"state": "open", "per_page": 50})
        return [
            pr["number"]
            for pr in prs
            if _SENTINEL_BRANCH_RE.match(pr["head"]["ref"])
        ]
    except Exception as exc:
        logger.warning("Could not list open PRs: %s", exc)
        return []


# ── Celery tasks ──────────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.worker.pr_tasks.review_and_merge_pr",
    queue="tasks_workspace",
    max_retries=1,
    default_retry_delay=120,
    soft_time_limit=720,
    time_limit=780,
)
def review_and_merge_pr(self, pr_number: int) -> dict:
    """
    Review a PR: resolve conflicts, wait for CI, approve and merge (or DM owner).
    Runs on tasks_workspace queue (concurrency=1) to avoid git conflicts.
    """
    try:
        return _review_pr_sync(pr_number)
    except Exception as exc:
        logger.error("review_and_merge_pr(%d) crashed: %s", pr_number, exc, exc_info=True)
        try:
            from app.integrations.slack_notifier import post_dm_sync
            post_dm_sync(
                f"❌ *PR review task crashed for PR #{pr_number}*\n"
                f"`{type(exc).__name__}: {exc}`\n"
                "_Manual review required._"
            )
        except Exception:
            pass
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="app.worker.pr_tasks.poll_open_sentinel_prs",
    queue="celery",
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def poll_open_sentinel_prs(self) -> dict:
    """
    Periodic fallback: find open sentinel/* PRs and trigger review tasks for any
    that aren't already being processed. Catches PRs that missed the webhook.
    """
    try:
        pr_numbers = _list_open_sentinel_prs()
        dispatched = []
        for pr_num in pr_numbers:
            review_and_merge_pr.apply_async(args=[pr_num], queue="tasks_workspace")
            dispatched.append(pr_num)
            logger.info("Dispatched review task for PR #%d", pr_num)
        return {"dispatched": dispatched}
    except Exception as exc:
        logger.error("poll_open_sentinel_prs failed: %s", exc)
        return {"error": str(exc)}
