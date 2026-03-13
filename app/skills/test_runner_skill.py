"""
TestRunnerSkill — run scoped pytest suites and return structured pass/fail results.

Intent: run_tests (internal; called from self-heal pipeline)
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

from app.config import get_settings
from app.skills.base import BaseSkill, SkillResult
from app.utils.shell import run as shell_run

logger = logging.getLogger(__name__)
settings = get_settings()


class TestRunnerSkill(BaseSkill):
    name = "test_runner"
    description = (
        "Run pytest on a specific test file or test ID and return structured "
        "pass/fail results. Used internally by the self-heal pipeline."
    )
    trigger_intents = ["run_tests"]

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        target = params.get("target", "tests/")
        repo_path = params.get("repo_path", "/root/sentinel-workspace")

        report_file = tempfile.mktemp(suffix=".json", prefix="pytest_report_")

        cmd = [
            "pytest",
            target,
            "--tb=short",
            "-q",
            "--no-header",
            f"--json-report",
            f"--json-report-file={report_file}",
        ]

        result = await shell_run(cmd, cwd=repo_path, timeout=120)

        # Parse JSON report if available
        report_path = Path(report_file)
        failures: list[dict] = []
        passed = 0
        failed = 0

        if report_path.exists():
            try:
                data = json.loads(report_path.read_text())
                summary = data.get("summary", {})
                passed = summary.get("passed", 0)
                failed = summary.get("failed", 0) + summary.get("error", 0)
                for test in data.get("tests", []):
                    if test.get("outcome") in ("failed", "error"):
                        failures.append(
                            {
                                "nodeid": test.get("nodeid", ""),
                                "message": (test.get("call", {}) or {}).get("longrepr", "")[:500],
                            }
                        )
                report_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Could not parse pytest JSON report: %s", exc)
                # Fall back to stdout parsing
                passed, failed, failures = _parse_stdout(result.stdout)
        else:
            passed, failed, failures = _parse_stdout(result.stdout)

        context = json.dumps(
            {
                "ok": result.ok,
                "passed": passed,
                "failed": failed,
                "failures": failures[:10],  # cap for context window
                "stdout": result.stdout[:1000],
            }
        )
        return SkillResult(context_data=context, is_error=not result.ok and failed == 0)


def _parse_stdout(stdout: str) -> tuple[int, int, list[dict]]:
    """Fallback: extract pass/fail counts from pytest stdout."""
    import re

    passed = 0
    failed = 0
    failures: list[dict] = []
    for line in stdout.splitlines():
        m = re.search(r"(\d+) passed", line)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", line)
        if m:
            failed = int(m.group(1))
        if line.startswith("FAILED "):
            failures.append({"nodeid": line[7:].strip(), "message": ""})
    return passed, failed, failures
