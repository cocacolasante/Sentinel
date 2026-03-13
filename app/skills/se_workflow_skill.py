"""
SE Workflow Skill — autonomous 5-phase Software Engineering pipeline.

Two modes:
  Mode 1 (sentinel self-work): Brainstorm / spec / plan / implement / review changes to
      Sentinel itself.  Output goes to /root/sentinel-workspace/se-tasks/{slug}/
  Mode 2 (new external project): Build full external client projects from scratch.
      Output goes to /root/projects/{slug}/

Each phase is handled by Claude Opus as an expert subagent.  Results are Markdown docs
committed to git so every decision and artefact is tracked.

Intents handled:
  se_brainstorm, se_spec, se_plan, se_implement, se_review,
  se_workflow, se_new_project, se_status
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import anthropic

from app.config import get_settings
from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

settings = get_settings()

# ── Path constants ────────────────────────────────────────────────────────────
_SENTINEL_WORKSPACE = "/root/sentinel-workspace"
_SE_TASKS_DIR       = "/root/sentinel-workspace/se-tasks"
_PROJECTS_DIR       = "/root/projects"

# ── DB helpers ────────────────────────────────────────────────────────────────

def _ensure_table() -> None:
    """Create the se_tasks table if it does not exist yet."""
    try:
        from app.db import postgres
        postgres.execute(
            """
            CREATE TABLE IF NOT EXISTS se_tasks (
                id          SERIAL PRIMARY KEY,
                slug        TEXT UNIQUE NOT NULL,
                title       TEXT,
                description TEXT,
                phase       TEXT,
                status      TEXT,
                project_type TEXT DEFAULT 'sentinel',
                task_dir    TEXT,
                git_cwd     TEXT,
                repo        TEXT,
                session_id  TEXT,
                created_at  TIMESTAMPTZ DEFAULT now(),
                updated_at  TIMESTAMPTZ DEFAULT now()
            )
            """
        )
    except Exception:
        pass  # non-fatal; DB may not be available in tests


def _upsert_task(slug: str, **kwargs) -> None:
    """Insert or update an se_tasks row. Uses parameterized queries to prevent SQL injection."""
    try:
        from app.db import postgres
        _ensure_table()
        items = [(k, str(v)) for k, v in kwargs.items() if v is not None]
        if not items:
            return
        keys = [k for k, _ in items]
        vals = [v for _, v in items]
        cols = ", ".join(keys)
        placeholders = ", ".join(["%s"] * len(vals))
        set_clause = ", ".join(f"{k} = %s" for k in keys)
        postgres.execute(
            f"""
            INSERT INTO se_tasks (slug, {cols})
            VALUES (%s, {placeholders})
            ON CONFLICT (slug) DO UPDATE SET {set_clause}, updated_at = now()
            """,
            [slug] + vals + vals,
        )
    except Exception:
        pass  # non-fatal


def _query_tasks() -> list[dict]:
    """Fetch all se_tasks rows."""
    try:
        from app.db import postgres
        _ensure_table()
        rows = postgres.fetch("SELECT * FROM se_tasks ORDER BY updated_at DESC")
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


# ── Core helpers ──────────────────────────────────────────────────────────────

def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]


def _resolve_dirs(slug: str, project_type: str) -> tuple[str, str]:
    """Return (task_dir, git_cwd) for the given slug and project_type."""
    if project_type == "project":
        task_dir = str(Path(_PROJECTS_DIR) / slug)
        git_cwd  = task_dir
    else:
        task_dir = str(Path(_SE_TASKS_DIR) / slug)
        git_cwd  = _SENTINEL_WORKSPACE
    return task_dir, git_cwd


def _read_doc(task_dir: str, filename: str, max_chars: int = 2000) -> str:
    """Safely read a prior-phase doc; return empty string if missing."""
    try:
        p = Path(task_dir) / filename
        if p.exists():
            content = p.read_text(encoding="utf-8")
            return content[:max_chars]
    except Exception:
        pass
    return ""


def _git_commit(task_dir: str, git_cwd: str, slug: str, phase: str) -> None:
    """Stage and commit phase output.  Failures are non-fatal."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=git_cwd,
            capture_output=True,
            check=False,
            timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", f"se-workflow({slug}): {phase} phase complete"],
            cwd=git_cwd,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except Exception:
        pass  # non-fatal


# ── SEWorkflowSkill ───────────────────────────────────────────────────────────


class SEWorkflowSkill(BaseSkill):
    name = "se_workflow"
    description = (
        "Autonomous 5-phase SE pipeline: brainstorm → spec → plan → implement → review. "
        "Mode 1: Sentinel self-improvement tasks saved to /root/sentinel-workspace/se-tasks/{slug}/. "
        "Mode 2: New external projects built from scratch into /root/projects/{slug}/. "
        "Each phase uses Claude Opus as an expert subagent and commits Markdown + code to git."
    )
    trigger_intents = [
        "se_brainstorm",
        "se_spec",
        "se_plan",
        "se_implement",
        "se_review",
        "se_workflow",
        "se_new_project",
        "se_status",
    ]
    approval_category = ApprovalCategory.STANDARD

    # ── LLM helper ────────────────────────────────────────────────────────────

    async def _llm(self, system: str, user: str) -> str:
        """Call Claude Opus as an expert subagent; return the text response."""
        api_key = settings.anthropic_api_key if hasattr(settings, "anthropic_api_key") else os.environ.get("ANTHROPIC_API_KEY", "")
        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text if msg.content else ""

    # ── Phase implementations ─────────────────────────────────────────────────

    async def _phase_brainstorm(
        self,
        task_dir: str,
        slug: str,
        title: str,
        description: str,
        repo: str,
        project_type: str,
    ) -> None:
        system = (
            "You are an expert software architect performing a brainstorming session. "
            "Produce thorough, actionable ideas.  Format your response as two sections "
            "separated by exactly the string '--- SPRINT ---' on its own line:\n"
            "1. BRAINSTORM: numbered list of ideas, risks, and open questions\n"
            "2. SPRINT PLAN: a short sprint plan with 3-5 prioritised user stories"
        )
        context = f"Project type: {project_type}\nRepo: {repo or 'N/A'}\nTitle: {title}\nDescription: {description}"
        response = await self._llm(system, context)
        parts = response.split("--- SPRINT ---", 1)
        brainstorm_content = parts[0].strip()
        sprint_content = parts[1].strip() if len(parts) > 1 else ""

        task_path = Path(task_dir)
        task_path.mkdir(parents=True, exist_ok=True)
        (task_path / "brainstorm.md").write_text(
            f"# Brainstorm — {title}\n\n{brainstorm_content}\n", encoding="utf-8"
        )
        if sprint_content:
            (task_path / "sprint.md").write_text(
                f"# Sprint Plan — {title}\n\n{sprint_content}\n", encoding="utf-8"
            )

    async def _phase_spec(
        self,
        task_dir: str,
        slug: str,
        title: str,
        description: str,
        repo: str,
        project_type: str,
    ) -> None:
        prior = _read_doc(task_dir, "brainstorm.md")
        system = (
            "You are a senior product manager writing a detailed functional specification. "
            "Cover: goals, non-goals, user stories, acceptance criteria, edge cases, "
            "data models, API contracts (if relevant), and out-of-scope items."
        )
        user = (
            f"Title: {title}\nDescription: {description}\nProject type: {project_type}\n"
            f"Repo: {repo or 'N/A'}\n\nPrior brainstorm:\n{prior}"
        )
        response = await self._llm(system, user)
        (Path(task_dir) / "spec.md").write_text(
            f"# Specification — {title}\n\n{response}\n", encoding="utf-8"
        )

    async def _phase_plan(
        self,
        task_dir: str,
        slug: str,
        title: str,
        description: str,
        repo: str,
        project_type: str,
    ) -> None:
        prior_spec = _read_doc(task_dir, "spec.md")
        prior_brainstorm = _read_doc(task_dir, "brainstorm.md")
        system = (
            "You are a senior software architect writing an implementation plan. "
            "Produce three sections separated by the exact delimiters on their own lines:\n"
            "'--- DECISIONS ---' separates the plan from architectural decision records.\n"
            "'--- NOTES ---' separates the ADRs from implementation notes / gotchas.\n\n"
            "Section 1 — PLAN: numbered implementation steps with file paths and scope.\n"
            "Section 2 — DECISIONS: ADRs for each major tech/design choice (format: Decision / Context / Consequences).\n"
            "Section 3 — NOTES: implementation notes, risks, dependencies, test strategy."
        )
        user = (
            f"Title: {title}\nDescription: {description}\nProject type: {project_type}\n"
            f"Repo: {repo or 'N/A'}\n\nSpec:\n{prior_spec}\n\nBrainstorm:\n{prior_brainstorm}"
        )
        response = await self._llm(system, user)

        parts = response.split("--- DECISIONS ---", 1)
        plan_content = parts[0].strip()
        remainder = parts[1] if len(parts) > 1 else ""
        decision_parts = remainder.split("--- NOTES ---", 1)
        decisions_content = decision_parts[0].strip()
        notes_content = decision_parts[1].strip() if len(decision_parts) > 1 else ""

        task_path = Path(task_dir)
        (task_path / "plan.md").write_text(
            f"# Implementation Plan — {title}\n\n{plan_content}\n", encoding="utf-8"
        )
        if decisions_content:
            (task_path / "decisions.md").write_text(
                f"# Architecture Decisions — {title}\n\n{decisions_content}\n", encoding="utf-8"
            )
        if notes_content:
            (task_path / "implementation-notes.md").write_text(
                f"# Implementation Notes — {title}\n\n{notes_content}\n", encoding="utf-8"
            )
        (task_path / "status.md").write_text(
            f"# Status — {title}\n\nPhase: plan\nStatus: complete\n", encoding="utf-8"
        )

    async def _phase_implement(
        self,
        task_dir: str,
        slug: str,
        title: str,
        description: str,
        repo: str,
        project_type: str,
    ) -> None:
        prior_plan = _read_doc(task_dir, "plan.md")
        prior_spec = _read_doc(task_dir, "spec.md")
        prior_notes = _read_doc(task_dir, "implementation-notes.md")
        system = (
            "You are a senior software engineer implementing the plan. "
            "Write all necessary code files. "
            "For each file, use this exact header format on its own line: '### path/to/file'\n"
            "followed by a markdown code block with the file's full content.\n"
            "After all files, include a section '### implementation.md' with a prose summary "
            "of what was built, how to run it, and any manual steps needed."
        )
        user = (
            f"Title: {title}\nDescription: {description}\nProject type: {project_type}\n"
            f"Repo: {repo or 'N/A'}\n\nPlan:\n{prior_plan}\n\nSpec:\n{prior_spec}\n"
            f"Notes:\n{prior_notes}"
        )
        response = await self._llm(system, user)

        task_path = Path(task_dir)
        code_dir = task_path / "code"
        code_dir.mkdir(parents=True, exist_ok=True)

        # Parse ### path/to/file sections
        file_pattern = re.compile(r"^###\s+(.+)$", re.MULTILINE)
        sections = file_pattern.split(response)
        # sections[0] = preamble, then alternating: path, content
        impl_summary = ""
        for i in range(1, len(sections) - 1, 2):
            file_path_str = sections[i].strip()
            content_block = sections[i + 1] if i + 1 < len(sections) else ""
            # Strip markdown code fence if present
            code_content = re.sub(r"^```[^\n]*\n", "", content_block.strip())
            code_content = re.sub(r"\n```\s*$", "", code_content)

            if file_path_str == "implementation.md":
                impl_summary = code_content.strip()
            else:
                out_path = code_dir / file_path_str
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(code_content, encoding="utf-8")

        (task_path / "implementation.md").write_text(
            f"# Implementation Summary — {title}\n\n{impl_summary or response[:2000]}\n",
            encoding="utf-8",
        )
        (task_path / "status.md").write_text(
            f"# Status — {title}\n\nPhase: implement\nStatus: complete\n", encoding="utf-8"
        )

    async def _phase_review(
        self,
        task_dir: str,
        slug: str,
        title: str,
        description: str,
        repo: str,
        project_type: str,
    ) -> None:
        brainstorm = _read_doc(task_dir, "brainstorm.md")
        spec       = _read_doc(task_dir, "spec.md")
        plan       = _read_doc(task_dir, "plan.md")
        impl       = _read_doc(task_dir, "implementation.md")
        notes      = _read_doc(task_dir, "implementation-notes.md")
        system = (
            "You are a principal engineer conducting a thorough code and design review. "
            "Evaluate correctness, security, maintainability, and completeness against the spec. "
            "End your audit with one of these exact verdict lines on its own line:\n"
            "VERDICT: APPROVED\n"
            "VERDICT: NEEDS WORK\n"
            "VERDICT: BLOCKED\n"
            "Include specific action items for NEEDS WORK or BLOCKED verdicts."
        )
        user = (
            f"Title: {title}\nDescription: {description}\n\n"
            f"Brainstorm:\n{brainstorm}\n\nSpec:\n{spec}\n\nPlan:\n{plan}\n\n"
            f"Implementation Summary:\n{impl}\n\nNotes:\n{notes}"
        )
        response = await self._llm(system, user)
        (Path(task_dir) / "audit.md").write_text(
            f"# Audit Report — {title}\n\n{response}\n", encoding="utf-8"
        )

    # ── Full pipeline ─────────────────────────────────────────────────────────

    async def _run_full_pipeline(
        self,
        slug: str,
        title: str,
        description: str,
        repo: str,
        project_type: str,
        task_dir: str,
        git_cwd: str,
    ) -> SkillResult:
        phases = [
            ("brainstorm", self._phase_brainstorm),
            ("spec",       self._phase_spec),
            ("plan",       self._phase_plan),
            ("implement",  self._phase_implement),
            ("review",     self._phase_review),
        ]
        for phase_name, phase_fn in phases:
            try:
                _upsert_task(slug, phase=phase_name, status="running")
                await phase_fn(task_dir, slug, title, description, repo, project_type)
                _git_commit(task_dir, git_cwd, slug, phase_name)
                _upsert_task(slug, phase=phase_name, status="done")
            except Exception as exc:
                error_msg = f"Phase {phase_name} failed: {exc}"
                try:
                    Path(task_dir).mkdir(parents=True, exist_ok=True)
                    (Path(task_dir) / "status.md").write_text(
                        f"# Status\n\nPhase: {phase_name}\nStatus: FAILED\nError: {exc}\n",
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                _upsert_task(slug, phase=phase_name, status="failed")
                return SkillResult(
                    context_data=f"[SE Workflow] Pipeline stopped at {phase_name}: {error_msg}",
                    skill_name=self.name,
                    is_error=True,
                )

        audit_content = _read_doc(task_dir, "audit.md", max_chars=500)
        return SkillResult(
            context_data=(
                f"[SE Workflow] Full pipeline complete for '{title}' ({slug}).\n"
                f"Artefacts written to: {task_dir}\n"
                f"Audit preview:\n{audit_content}"
            ),
            skill_name=self.name,
        )

    # ── New-project initialisation ────────────────────────────────────────────

    async def _init_new_project(self, slug: str, title: str, description: str) -> str:
        """Create /root/projects/{slug}/, README, git init.  Returns task_dir."""
        task_dir = str(Path(_PROJECTS_DIR) / slug)
        path = Path(task_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / "README.md").write_text(
            f"# {title}\n\n{description}\n\n_Generated by Sentinel SE Workflow_\n",
            encoding="utf-8",
        )
        try:
            subprocess.run(["git", "init"], cwd=task_dir, capture_output=True, check=False, timeout=30)
            subprocess.run(["git", "add", "-A"], cwd=task_dir, capture_output=True, check=False, timeout=30)
            subprocess.run(
                ["git", "commit", "-m", f"init: {title}"],
                cwd=task_dir,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except Exception:
            pass

        # Optionally create GitHub repo if token is available
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if github_token:
            try:
                import httpx
                resp = httpx.post(
                    "https://api.github.com/user/repos",
                    json={"name": slug, "description": description, "private": True, "auto_init": False},
                    headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github+json"},
                    timeout=15,
                )
                if resp.status_code in (200, 201):
                    remote_url = resp.json().get("clone_url", "")
                    if remote_url:
                        subprocess.run(
                            ["git", "remote", "add", "origin", remote_url],
                            cwd=task_dir,
                            capture_output=True,
                            check=False,
                            timeout=15,
                        )
            except Exception:
                pass

        return task_dir

    # ── Status query ──────────────────────────────────────────────────────────

    async def _se_status(self) -> SkillResult:
        rows = _query_tasks()
        if not rows:
            return SkillResult(
                context_data="[SE Workflow] No SE tasks found in the database.",
                skill_name=self.name,
            )
        lines = ["[SE Workflow] Active SE tasks:\n"]
        for row in rows:
            status_icon = {"done": "✅", "running": "🔄", "failed": "❌"}.get(row.get("status", ""), "•")
            lines.append(
                f"{status_icon} #{row.get('id')} **{row.get('title', row.get('slug'))}** "
                f"— phase: {row.get('phase', '?')} | status: {row.get('status', '?')} "
                f"| type: {row.get('project_type', 'sentinel')}"
            )
        return SkillResult(context_data="\n".join(lines), skill_name=self.name)

    # ── Single-phase dispatch ─────────────────────────────────────────────────

    async def _run_single_phase(
        self,
        intent: str,
        slug: str,
        title: str,
        description: str,
        repo: str,
        project_type: str,
        task_dir: str,
        git_cwd: str,
    ) -> SkillResult:
        phase_map = {
            "se_brainstorm": ("brainstorm", self._phase_brainstorm),
            "se_spec":       ("spec",       self._phase_spec),
            "se_plan":       ("plan",       self._phase_plan),
            "se_implement":  ("implement",  self._phase_implement),
            "se_review":     ("review",     self._phase_review),
        }
        phase_name, phase_fn = phase_map[intent]
        try:
            Path(task_dir).mkdir(parents=True, exist_ok=True)
            _upsert_task(
                slug,
                title=title,
                description=description,
                phase=phase_name,
                status="running",
                project_type=project_type,
                task_dir=task_dir,
                git_cwd=git_cwd,
                repo=repo,
            )
            await phase_fn(task_dir, slug, title, description, repo, project_type)
            _git_commit(task_dir, git_cwd, slug, phase_name)
            _upsert_task(slug, phase=phase_name, status="done")
            doc_preview = _read_doc(task_dir, f"{phase_name}.md", max_chars=400)
            return SkillResult(
                context_data=(
                    f"[SE Workflow] {phase_name} phase complete for '{title}'.\n"
                    f"Output directory: {task_dir}\nPreview:\n{doc_preview}"
                ),
                skill_name=self.name,
            )
        except Exception as exc:
            _upsert_task(slug, phase=phase_name, status="failed")
            return SkillResult(
                context_data=f"[SE Workflow] {phase_name} phase failed: {exc}",
                skill_name=self.name,
                is_error=True,
            )

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        intent = params.get("intent", "")

        # Status query — no additional params needed
        if intent == "se_status":
            return await self._se_status()

        # Extract common parameters
        title       = params.get("title", "") or original_message[:80]
        description = params.get("description", "") or original_message
        repo        = params.get("repo", "")
        slug        = params.get("slug", "") or _slugify(title)
        project_type = params.get("project_type", "sentinel")

        # New external project
        if intent == "se_new_project":
            project_type = "project"
            try:
                task_dir = await self._init_new_project(slug, title, description)
                git_cwd  = task_dir
                _upsert_task(
                    slug,
                    title=title,
                    description=description,
                    phase="init",
                    status="running",
                    project_type=project_type,
                    task_dir=task_dir,
                    git_cwd=git_cwd,
                    repo=repo,
                )
                return await self._run_full_pipeline(slug, title, description, repo, project_type, task_dir, git_cwd)
            except Exception as exc:
                return SkillResult(
                    context_data=f"[SE Workflow] Failed to init new project '{slug}': {exc}",
                    skill_name=self.name,
                    is_error=True,
                )

        # Resolve paths for sentinel or project type
        task_dir, git_cwd = _resolve_dirs(slug, project_type)

        # Full pipeline (all 5 phases)
        if intent == "se_workflow":
            try:
                Path(task_dir).mkdir(parents=True, exist_ok=True)
                _upsert_task(
                    slug,
                    title=title,
                    description=description,
                    phase="brainstorm",
                    status="running",
                    project_type=project_type,
                    task_dir=task_dir,
                    git_cwd=git_cwd,
                    repo=repo,
                )
            except Exception:
                pass
            return await self._run_full_pipeline(slug, title, description, repo, project_type, task_dir, git_cwd)

        # Single phase
        if intent in ("se_brainstorm", "se_spec", "se_plan", "se_implement", "se_review"):
            return await self._run_single_phase(
                intent, slug, title, description, repo, project_type, task_dir, git_cwd
            )

        return SkillResult(
            context_data=f"[SE Workflow] Unknown intent: {intent}",
            skill_name=self.name,
            is_error=True,
        )
