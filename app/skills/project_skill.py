"""
ProjectSkill — tell Sentinel what to build and it handles the rest.

Flow:
  create → scaffold project dir → queue build_project Celery task
  build  → (re)trigger build agent loop
  deploy → confirm → provision IONOS server → SSH deploy → report IP
  status → DB + server state
  list   → all projects

Projects live at:  /root/sentinel-workspace/projects/{slug}/
Each project gets: start.sh (entrypoint), README.md, .gitignore

IONOS deploy requires:
  IONOS_SSH_PUBLIC_KEY  — public key injected into the server at provision
  IONOS_SSH_PRIVATE_KEY — private key content (env var, written to temp file)
  OR IONOS_SSH_PRIVATE_KEY_PATH — path to the private key file
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)

_WORKSPACE = "/root/sentinel-workspace" if os.path.isdir("/root/sentinel-workspace") else "/app"
_PROJECTS = f"{_WORKSPACE}/projects"

_STATUS_EMOJI = {
    "queued": "🕐",
    "building": "🔨",
    "built": "✅",
    "deploying": "🚀",
    "deployed": "🌐",
    "failed": "❌",
}

_IONOS_LOCATIONS = {
    "us": "us/las",
    "us-east": "us/ewr",
    "eu": "de/fra",
    "de": "de/fra",
    "uk": "gb/lhr",
    "de/fra": "de/fra",
    "us/las": "us/las",
    "gb/lhr": "gb/lhr",
}

_TECH_LABELS = {
    "python": "Python / FastAPI",
    "fastapi": "Python / FastAPI",
    "flask": "Python / Flask",
    "django": "Python / Django",
    "node": "Node.js / Express",
    "nodejs": "Node.js / Express",
    "express": "Node.js / Express",
    "react": "React (Node.js)",
    "nextjs": "Next.js",
    "next": "Next.js",
    "go": "Go",
    "golang": "Go",
    "rust": "Rust",
    "static": "Static HTML/CSS/JS",
    "html": "Static HTML/CSS/JS",
}


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:60]


def _ensure_table() -> None:
    from app.db import postgres

    postgres.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id              SERIAL PRIMARY KEY,
            name            VARCHAR(255)  NOT NULL,
            slug            VARCHAR(255)  UNIQUE NOT NULL,
            description     TEXT,
            tech_stack      VARCHAR(100)  DEFAULT 'python',
            path            VARCHAR(500),
            status          VARCHAR(50)   DEFAULT 'queued',
            build_log       TEXT,
            ionos_dc_id     VARCHAR(255),
            ionos_server_id VARCHAR(255),
            ionos_nic_id    VARCHAR(255),
            deploy_ip       VARCHAR(100),
            deploy_url      VARCHAR(500),
            session_id      VARCHAR(255),
            slack_channel   VARCHAR(100),
            slack_thread_ts VARCHAR(100),
            created_at      TIMESTAMP     DEFAULT NOW(),
            updated_at      TIMESTAMP     DEFAULT NOW()
        )
        """
    )


def _fmt_project(row: dict) -> str:
    emoji = _STATUS_EMOJI.get(row.get("status", ""), "•")
    label = _TECH_LABELS.get(row.get("tech_stack", ""), row.get("tech_stack", ""))
    status = row.get("status", "unknown")
    lines = [
        f"{emoji} **#{row['id']} — {row['name']}** ({label})",
        f"   Status: {status}  |  Path: `{row.get('path', '?')}`",
    ]
    if row.get("deploy_ip"):
        lines.append(f"   🌐 Server IP: `{row['deploy_ip']}`")
    if row.get("deploy_url"):
        lines.append(f"   URL: {row['deploy_url']}")
    if row.get("description"):
        desc = (row["description"] or "")[:120]
        lines.append(f"   {desc}")
    return "\n".join(lines)


class ProjectSkill(BaseSkill):
    name = "project"
    description = (
        "Create and manage full software projects end-to-end: React frontends, FastAPI backends, "
        "Go services, static sites, SaaS dashboards. Use when Anthony says 'create project', "
        "'start a new project', 'build a [type] project', 'deploy project [name]', "
        "'project status', or 'list my projects'. Scaffolds complete code including frontend, "
        "backend, Docker, and CI/CD. NOT for: modifying Sentinel itself (use se_workflow) or "
        "small code edits (use repo_write)."
    )
    trigger_intents = [
        "project_create",
        "project_build",
        "project_deploy",
        "project_status",
        "project_list",
    ]
    approval_category = ApprovalCategory.NONE  # IONOS deploy uses BREAKING per-action

    def is_available(self) -> bool:
        return True

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        _ensure_table()
        action = (params.get("action") or "create").strip().lower()

        if action == "list":
            return await self._list()
        if action in ("status", "get"):
            return await self._status(params)
        if action == "build":
            return await self._rebuild(params, original_message)
        if action == "deploy":
            return await self._deploy(params, original_message)
        # default → create
        return await self._create(params, original_message)

    # ── create ─────────────────────────────────────────────────────────────────

    async def _create(self, params: dict, original_message: str) -> SkillResult:
        from app.db import postgres

        name = (params.get("name") or params.get("project_name") or "").strip()
        description = (params.get("description") or original_message or "").strip()
        tech_stack = (params.get("tech_stack") or "python").strip().lower()
        auto_deploy = str(params.get("deploy", "false")).lower() in ("true", "1", "yes")
        session_id = (params.get("session_id") or "").strip()
        ionos_loc = _IONOS_LOCATIONS.get((params.get("ionos_location") or "eu").lower().strip(), "de/fra")

        if not name:
            return SkillResult(
                context_data=("[project_create requires a project name. Ask: what should the project be called?]"),
                skill_name=self.name,
            )

        slug = _slugify(name)
        path = f"{_PROJECTS}/{slug}"

        # Slack context for reporting back
        slack_channel = slack_thread_ts = None
        if session_id:
            try:
                from app.memory.redis_client import RedisMemory

                ctx = RedisMemory().get_slack_context(session_id)
                if ctx:
                    slack_channel = ctx.get("channel")
                    slack_thread_ts = ctx.get("thread_ts")
            except Exception as e:
                logger.warning("ProjectSkill: failed to fetch Slack context: %s", e)

        # Insert project row
        try:
            row = postgres.execute_one(
                """
                INSERT INTO projects
                    (name, slug, description, tech_stack, path,
                     status, session_id, slack_channel, slack_thread_ts)
                VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    description = EXCLUDED.description,
                    tech_stack  = EXCLUDED.tech_stack,
                    status      = 'queued',
                    updated_at  = NOW()
                RETURNING id, name, slug, status, tech_stack, path
                """,
                (
                    name,
                    slug,
                    description,
                    tech_stack,
                    path,
                    session_id or None,
                    slack_channel,
                    slack_thread_ts,
                ),
            )
        except Exception as exc:
            return SkillResult(
                context_data=f"[project_create DB error: {exc}]",
                skill_name=self.name,
            )

        project_id = row["id"]

        # Create the project directory
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            logger.warning("ProjectSkill: could not create project directory %s: %s", path, e)

        # Queue the build Celery task
        from app.worker.project_tasks import build_project

        result = build_project.apply_async(
            args=[project_id],
            kwargs={"auto_deploy": auto_deploy, "ionos_location": ionos_loc},
            queue="tasks_workspace",
        )
        celery_id = result.id

        tech_label = _TECH_LABELS.get(tech_stack, tech_stack)
        deploy_note = (
            "\n🚀 Auto-deploy to IONOS staging server is **enabled** — "
            "once the build passes I'll provision a server and send you the IP."
            if auto_deploy
            else f'\n💡 To deploy to an IONOS staging server later, say "deploy project {name}".'
        )

        context = (
            f"Project **#{project_id} — {name}** created!\n\n"
            f"• Tech stack: {tech_label}\n"
            f"• Path: `{path}`\n"
            f"• Status: 🔨 building (Celery ID `{celery_id[:8]}…`)\n"
            f"{deploy_note}\n\n"
            "I'm writing the code in the background — "
            "I'll post the results to this Slack thread when the build is done."
        )
        return SkillResult(context_data=context, skill_name=self.name)

    # ── build (re-trigger) ─────────────────────────────────────────────────────

    async def _rebuild(self, params: dict, original_message: str) -> SkillResult:
        from app.db import postgres

        project_id = params.get("project_id") or params.get("id")
        slug = (params.get("slug") or params.get("name") or "").strip()

        row = None
        if project_id:
            row = postgres.execute_one("SELECT * FROM projects WHERE id=%s", (int(project_id),))
        elif slug:
            row = postgres.execute_one("SELECT * FROM projects WHERE slug=%s", (_slugify(slug),))

        if not row:
            return SkillResult(
                context_data="[No project found. Use project_list to see available projects.]",
                skill_name=self.name,
            )

        postgres.execute("UPDATE projects SET status='queued', updated_at=NOW() WHERE id=%s", (row["id"],))

        from app.worker.project_tasks import build_project

        result = build_project.apply_async(args=[row["id"]], queue="tasks_workspace")

        return SkillResult(
            context_data=(
                f"Re-queued build for **#{row['id']} — {row['name']}**.\n"
                f"Celery ID `{result.id[:8]}…` — I'll post results to Slack when done."
            ),
            skill_name=self.name,
        )

    # ── deploy ─────────────────────────────────────────────────────────────────

    async def _deploy(self, params: dict, original_message: str) -> SkillResult:
        from app.db import postgres

        project_id = params.get("project_id") or params.get("id")
        slug = (params.get("slug") or params.get("name") or "").strip()

        row = None
        if project_id:
            row = postgres.execute_one("SELECT * FROM projects WHERE id=%s", (int(project_id),))
        elif slug:
            row = postgres.execute_one("SELECT * FROM projects WHERE slug=%s", (_slugify(slug),))

        if not row:
            return SkillResult(
                context_data="[No project found. Specify project_id or name.]",
                skill_name=self.name,
            )

        if row.get("status") not in ("built", "deployed", "failed"):
            return SkillResult(
                context_data=(
                    f"Project **#{row['id']} — {row['name']}** is currently `{row.get('status')}`. "
                    "Wait for the build to complete before deploying."
                ),
                skill_name=self.name,
            )

        ionos_loc = _IONOS_LOCATIONS.get((params.get("ionos_location") or "eu").lower().strip(), "de/fra")
        cores = max(1, int(params.get("server_cores", 2)))
        ram_gb = max(1, int(params.get("server_ram_gb", 2)))
        ram_mb = ram_gb * 1024

        loc_label = {
            "de/fra": "🇩🇪 Frankfurt",
            "us/las": "🇺🇸 Las Vegas",
            "gb/lhr": "🇬🇧 London",
            "us/ewr": "🇺🇸 Newark",
        }.get(ionos_loc, ionos_loc)

        summary = (
            f"Deploy **#{row['id']} — {row['name']}** to a new IONOS server:\n\n"
            f"• Location: {loc_label} (`{ionos_loc}`)\n"
            f"• Server: {cores} cores, {ram_gb} GB RAM\n"
            f"• OS: Ubuntu 22.04 LTS\n"
            f"• Action: provision server → SSH install → deploy → report IP\n\n"
            "⚠️ This creates a **billable IONOS server**. "
            "Reply **confirm** to proceed or **cancel** to abort."
        )

        pending = {
            "intent": "project_deploy",
            "action": "project_deploy",
            "params": {
                "project_id": row["id"],
                "ionos_location": ionos_loc,
                "server_cores": cores,
                "server_ram_mb": ram_mb,
                "session_id": params.get("session_id", ""),
            },
            "original": original_message,
        }

        self.approval_category = ApprovalCategory.BREAKING  # type: ignore[assignment]
        return SkillResult(
            context_data=summary,
            pending_action=pending,
            skill_name=self.name,
        )

    # ── status ─────────────────────────────────────────────────────────────────

    async def _status(self, params: dict) -> SkillResult:
        from app.db import postgres

        project_id = params.get("project_id") or params.get("id")
        slug = (params.get("slug") or params.get("name") or "").strip()

        if project_id:
            row = postgres.execute_one("SELECT * FROM projects WHERE id=%s", (int(project_id),))
        elif slug:
            row = postgres.execute_one("SELECT * FROM projects WHERE slug=%s", (_slugify(slug),))
        else:
            return await self._list()

        if not row:
            return SkillResult(context_data="[Project not found.]", skill_name=self.name)

        lines = [_fmt_project(row)]
        if row.get("build_log"):
            tail = (row["build_log"] or "")[-800:].strip()
            lines.append(f"\n**Build log (tail):**\n```\n{tail}\n```")

        return SkillResult(context_data="\n".join(lines), skill_name=self.name)

    # ── list ───────────────────────────────────────────────────────────────────

    async def _list(self) -> SkillResult:
        from app.db import postgres

        rows = postgres.execute("SELECT * FROM projects ORDER BY created_at DESC LIMIT 20")
        if not rows:
            return SkillResult(
                context_data="No projects yet. Say 'create a project' to get started!",
                skill_name=self.name,
            )
        lines = [f"**Projects** ({len(rows)} found)\n"]
        for r in rows:
            lines.append(_fmt_project(r))
        return SkillResult(context_data="\n".join(lines), skill_name=self.name)
