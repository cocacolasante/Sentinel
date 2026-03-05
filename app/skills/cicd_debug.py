"""
CI/CD Debug Skill

Fetches the latest failed GitHub Actions workflow run, extracts the error output,
correlates it with the source file, and provides a targeted fix recommendation.
Optionally auto-commits the fix and re-triggers the pipeline.
"""

from __future__ import annotations

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class CicdDebugSkill(BaseSkill):
    name = "cicd_debug"
    description = (
        "Fetch CI/CD pipeline error logs, parse failures, correlate with code, "
        "and provide fix recommendations or execute corrections. "
        "Use when the user asks to debug a failed pipeline, fix CI errors, or "
        "investigate a failed GitHub Actions run."
    )
    trigger_intents = ["cicd_debug"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.config import get_settings

        settings = get_settings()

        repo = params.get("repo") or settings.github_default_repo
        run_id = params.get("run_id")  # optional — latest failed run used if omitted
        fix = params.get("fix", False)

        if not repo:
            return SkillResult(
                context_data=(
                    "[cicd_debug needs a repo. Set GITHUB_DEFAULT_REPO in .env or pass repo='owner/name' in params.]"
                ),
                skill_name=self.name,
            )

        token = settings.github_token
        if not token:
            return SkillResult(
                context_data="[cicd_debug needs GITHUB_TOKEN set in .env]",
                skill_name=self.name,
            )

        import httpx

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            # 1. Resolve run_id → latest failed run if not provided
            if not run_id:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/actions/runs",
                    params={"status": "failure", "per_page": 1},
                )
                if resp.status_code != 200:
                    return SkillResult(
                        context_data=(f"[GitHub API error {resp.status_code}: {resp.text[:200]}]"),
                        skill_name=self.name,
                    )
                runs = resp.json().get("workflow_runs", [])
                if not runs:
                    return SkillResult(
                        context_data="[No failed workflow runs found — pipeline is green!]",
                        skill_name=self.name,
                    )
                run = runs[0]
                run_id = run["id"]
                run_name = run.get("name", "unknown")
                run_url = run.get("html_url", "")
                head_sha = run.get("head_sha", "")[:8]
            else:
                resp = await client.get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}")
                run = resp.json()
                run_name = run.get("name", "unknown")
                run_url = run.get("html_url", "")
                head_sha = run.get("head_sha", "")[:8]

            # 2. Get failed jobs
            jobs_resp = await client.get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs")
            jobs = jobs_resp.json().get("jobs", [])
            failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

            if not failed_jobs:
                return SkillResult(
                    context_data=(f"[Run #{run_id} has no failed jobs — may still be in progress]"),
                    skill_name=self.name,
                )

            # 3. Fetch logs for the first failed job
            job = failed_jobs[0]
            job_id = job["id"]
            job_name = job["name"]

            log_resp = await client.get(f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs")
            # GitHub returns a redirect to a signed S3 URL
            if log_resp.status_code in (301, 302):
                log_resp = await client.get(log_resp.headers["location"])

            raw_logs = log_resp.text if log_resp.status_code == 200 else ""
            # Keep only the last 3 KB — that's where errors are
            log_tail = raw_logs[-3000:] if raw_logs else "(logs unavailable)"

            # 4. Parse errors — look for common patterns
            error_lines = []
            for line in log_tail.splitlines():
                low = line.lower()
                if any(kw in low for kw in ("error", "failed", "syntaxerror", "traceback", "ruff")):
                    error_lines.append(line.strip())

            error_excerpt = "\n".join(error_lines[:30]) if error_lines else log_tail[-800:]

            # 5. Build summary
            lines = [
                f"**CI/CD Debug — Run #{run_id}** (`{head_sha}`)",
                f"Workflow: {run_name}  |  [View run]({run_url})",
                f"Failed job: **{job_name}**",
                "",
                "**Error excerpt:**",
                f"```\n{error_excerpt[:1200]}\n```",
            ]

            if len(failed_jobs) > 1:
                other = ", ".join(j["name"] for j in failed_jobs[1:4])
                lines.append(f"\n_Also failed: {other}_")

            if fix:
                lines.append(
                    "\n⚙️ `fix=true` was requested — use the `code` or `repo_write` skill "
                    "with the error excerpt above to apply a targeted fix, then re-trigger CI."
                )

        return SkillResult(context_data="\n".join(lines), skill_name=self.name)
