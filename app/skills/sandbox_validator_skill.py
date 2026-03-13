"""
SandboxValidatorSkill — apply a patch in a temp copy of the repo, run targeted tests,
report pass/fail without touching the live codebase.

Intent: validate_patch (internal; called from self-heal pipeline)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile

from app.config import get_settings
from app.skills.base import BaseSkill, SkillResult
from app.utils.shell import run as shell_run

logger = logging.getLogger(__name__)
settings = get_settings()


class SandboxValidatorSkill(BaseSkill):
    name = "sandbox_validator"
    description = (
        "Apply a unified diff patch in an isolated temp directory and run the failing tests "
        "to verify the patch works. Used internally by the self-heal pipeline."
    )
    trigger_intents = ["validate_patch"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        diff_text: str = params.get("diff", "")
        test_ids: list[str] = params.get("test_ids", [])
        repo_path: str = params.get("repo_path", "/root/sentinel-workspace")

        if not diff_text:
            return SkillResult(context_data='{"ok": false, "error": "No diff provided"}', is_error=True)

        tmpdir = tempfile.mkdtemp(prefix="sentinel_sandbox_")
        try:
            # Copy repo to sandbox
            shutil.copytree(repo_path, tmpdir, dirs_exist_ok=True)

            # Apply patch
            patch_result = subprocess.run(
                ["patch", "-p1"],
                input=diff_text.encode(),
                cwd=tmpdir,
                capture_output=True,
                timeout=30,
            )
            if patch_result.returncode != 0:
                stderr = patch_result.stderr.decode(errors="replace")[:500]
                return SkillResult(
                    context_data=json.dumps({"ok": False, "error": f"patch failed: {stderr}"}),
                    is_error=True,
                )

            # Run tests in sandbox
            targets = test_ids if test_ids else ["tests/"]
            cmd = ["pytest"] + targets + ["-q", "--no-header", "--tb=short"]
            result = await shell_run(cmd, cwd=tmpdir, timeout=90)

            return SkillResult(
                context_data=json.dumps(
                    {
                        "ok": result.ok,
                        "returncode": result.returncode,
                        "stdout": result.stdout[:1000],
                        "stderr": result.stderr[:500],
                    }
                )
            )

        except subprocess.TimeoutExpired:
            return SkillResult(
                context_data=json.dumps({"ok": False, "error": "patch command timed out"}),
                is_error=True,
            )
        except Exception as exc:
            logger.error("SandboxValidatorSkill error: %s", exc)
            return SkillResult(
                context_data=json.dumps({"ok": False, "error": str(exc)}),
                is_error=True,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
