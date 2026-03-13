"""
ProjectScaffoldSkill — scaffold a complete deployable project using Claude Opus
with extended thinking.

Writes to /root/projects/{slug}/, runs git init + initial commit,
optionally creates a GitHub repo, and logs to sentinel_audit.

Intent: project_scaffold
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

import anthropic

from app.config import get_settings
from app.db import postgres
from app.skills.base import BaseSkill, SkillResult

logger = logging.getLogger(__name__)
settings = get_settings()

_SCAFFOLD_SYSTEM = (
    "You are a senior software architect. Given a project description, produce a complete "
    "project scaffold as JSON.\n\n"
    "Return ONLY valid JSON with this structure:\n"
    "{\n"
    '  "slug": "kebab-case-project-name",\n'
    '  "description": "one-line description",\n'
    '  "tech_stack": "FastAPI" | "Node.js" | "React" | "Static HTML" | ...,\n'
    '  "files": [\n'
    '    {"path": "relative/path/file.py", "content": "file content here"},\n'
    "    ...\n"
    "  ]\n"
    "}\n\n"
    "Include README.md, main source files, requirements.txt/package.json, "
    "Dockerfile and docker-compose.yml for web projects. "
    "Write production-ready code with proper error handling."
)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:50]


class ProjectScaffoldSkill(BaseSkill):
    name = "project_scaffold"
    description = (
        "Scaffold a complete deployable project from scratch using Claude Opus with extended thinking. "
        "Creates file tree, source files, Dockerfile, README. "
        "Use for: 'scaffold a project', 'build me a X', 'create a new Y service'. "
        "NOT for modifying the existing Sentinel codebase."
    )
    trigger_intents = ["project_scaffold"]

    def is_available(self) -> bool:
        return bool(settings.anthropic_api_key)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        description = params.get("description") or original_message
        slug = params.get("slug") or _slugify(description)
        session_id = params.get("session_id", "")
        create_github = params.get("create_github_repo", False)

        projects_root = Path("/root/projects")
        projects_root.mkdir(parents=True, exist_ok=True)
        project_dir = projects_root / slug

        if project_dir.exists():
            return SkillResult(
                context_data=f"Project directory already exists: {project_dir}",
                is_error=True,
            )

        # Call Opus with extended thinking
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        try:
            response = client.messages.create(
                model=settings.model_opus,
                max_tokens=16000,
                thinking={"type": "enabled", "budget_tokens": 8000},
                system=_SCAFFOLD_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": f"Scaffold this project: {description}",
                    }
                ],
            )
        except Exception as exc:
            logger.error("Opus scaffold call failed: %s", exc)
            return SkillResult(context_data=f"LLM call failed: {exc}", is_error=True)

        # Extract JSON from response (skip thinking blocks)
        raw_json = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_json = block.text.strip()
                break

        try:
            scaffold = json.loads(raw_json)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code block
            match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw_json)
            if match:
                try:
                    scaffold = json.loads(match.group(1))
                except json.JSONDecodeError as exc:
                    return SkillResult(
                        context_data=f"Could not parse scaffold JSON: {exc}\n\nRaw:\n{raw_json[:500]}",
                        is_error=True,
                    )
            else:
                return SkillResult(
                    context_data=f"LLM did not return valid JSON.\n\nRaw:\n{raw_json[:500]}",
                    is_error=True,
                )

        # Use slug from response if provided
        if scaffold.get("slug"):
            slug = scaffold["slug"]
            project_dir = projects_root / slug

        files: list[dict] = scaffold.get("files", [])
        if not files:
            return SkillResult(context_data="LLM returned no files in scaffold.", is_error=True)

        # Write files
        project_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for f in files:
            rel_path = f.get("path", "")
            content = f.get("content", "")
            if not rel_path:
                continue
            target = project_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            written.append(rel_path)

        # Git init + initial commit
        try:
            subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=project_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"feat: initial scaffold — {description[:80]}"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "Sentinel", "GIT_AUTHOR_EMAIL": "sentinel@ai", "GIT_COMMITTER_NAME": "Sentinel", "GIT_COMMITTER_EMAIL": "sentinel@ai"},
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("git init/commit failed: %s", exc.stderr)

        # sentinel_audit row
        try:
            postgres.execute(
                """
                INSERT INTO sentinel_audit (session_id, action, target, outcome, detail)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    "scaffold",
                    str(project_dir),
                    "created",
                    json.dumps({"files": written, "tech_stack": scaffold.get("tech_stack", "")}),
                ),
            )
        except Exception as exc:
            logger.warning("sentinel_audit insert failed: %s", exc)

        summary = (
            f"Project scaffolded at `{project_dir}`\n"
            f"Tech stack: {scaffold.get('tech_stack', 'unknown')}\n"
            f"Files written: {len(written)}\n"
            + "\n".join(f"  • {p}" for p in written[:20])
        )
        return SkillResult(context_data=summary)
