"""
DeploySkill — self-deploy the Brain.

Workflow (triggered after code changes are committed and pushed):
  1. Brain responds with a confirmation prompt.
  2. User replies "confirm".
  3. Celery worker: git pull → docker compose build brain → docker compose up -d brain.
  4. Slack notification posted when done.

The brain container will be briefly offline (~60 s) during the rebuild.
The Celery worker, PostgreSQL, Redis, and other services stay up the whole time.

Intent: deploy
"""

from __future__ import annotations

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class DeploySkill(BaseSkill):
    name = "deploy"
    description = (
        "Deploy applications to production: trigger Docker Compose rebuilds, restart services, "
        "run deployment scripts, hot-reload configs. Use when Anthony says 'deploy', "
        "'deploy to production', 'restart the app', 'rebuild and deploy', 'hot reload', "
        "or 'push to prod'. "
        "NOT for: CI/CD pipeline triggers (use cicd_trigger), reading deploy status (use cicd_read), "
        "or raw shell commands (use server_shell)."
    )
    trigger_intents = ["deploy"]
    requires_confirmation = True
    approval_category = ApprovalCategory.BREAKING  # always requires confirmation

    def is_available(self) -> bool:
        return True  # Celery worker handles the actual Docker calls

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        reason = params.get("reason", "")
        pending = {
            "intent": "deploy",
            "action": "deploy_brain",
            "params": {"reason": reason or original_message[:120]},
            "original": original_message,
        }
        context = (
            "About to **rebuild the Brain Docker image** and **restart the brain container**.\n\n"
            + (f"Reason: _{reason}_\n\n" if reason else "")
            + "What happens:\n"
            "  1. Celery worker pulls latest code from GitHub\n"
            "  2. Rebuilds the `sentinel-brain` Docker image (~45–90 s)\n"
            "  3. Hot-swaps the running brain container with the new image\n\n"
            "⚠️ The brain will be **offline for ~60 seconds** during the restart.\n"
            "All other services (Celery, Postgres, Redis, Grafana) stay up.\n\n"
            "Reply **confirm** to start the deploy or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
