"""
PatchGeneratorSkill — given failing test output, generate a minimal unified diff
using Claude Sonnet, validated with unidiff.

Intent: generate_patch (internal; called from self-heal pipeline)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic

from app.config import get_settings
from app.skills.base import BaseSkill, SkillResult
from app.skills.code_index_skill import search_symbols

logger = logging.getLogger(__name__)
settings = get_settings()

_SYSTEM = (
    "You are a patch engineer. Given failing test output and the relevant source files, "
    "produce a minimal unified diff that fixes the failures.\n"
    "Return ONLY a unified diff in standard format (--- a/... +++ b/... @@ ... lines). "
    "No prose, no explanations, no code blocks — just the raw diff text."
)


class PatchGeneratorSkill(BaseSkill):
    name = "patch_generator"
    description = (
        "Generate a unified diff patch to fix failing tests. "
        "Queries Qdrant for relevant files, then calls Claude Sonnet to produce a minimal fix."
    )
    trigger_intents = ["generate_patch"]

    def is_available(self) -> bool:
        return bool(settings.anthropic_api_key)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        failures_raw = params.get("failures", "[]")
        repo_path = params.get("repo_path", "/root/sentinel-workspace")

        if isinstance(failures_raw, str):
            try:
                failures = json.loads(failures_raw)
            except json.JSONDecodeError:
                failures = [{"nodeid": "", "message": failures_raw}]
        else:
            failures = failures_raw

        if not failures:
            return SkillResult(context_data="No failures provided — nothing to patch.")

        # Build query from failure messages
        failure_text = "\n".join(
            f"{f.get('nodeid', '')}: {f.get('message', '')}" for f in failures[:5]
        )

        # Search Qdrant for relevant symbols
        relevant = search_symbols(failure_text, limit=5)

        # Read relevant source files
        source_context = ""
        seen_files: set[str] = set()
        root = Path(repo_path)
        for hit in relevant:
            rel_path = hit.get("file", "")
            if not rel_path or rel_path in seen_files:
                continue
            seen_files.add(rel_path)
            full_path = root / rel_path
            try:
                if full_path.exists():
                    content = full_path.read_text(errors="replace")[:3000]
                    source_context += f"\n--- File: {rel_path} ---\n{content}\n"
            except Exception as exc:
                logger.debug("Could not read %s: %s", rel_path, exc)

        if not source_context:
            # Fall back: try to read test file itself for context
            first_failure = failures[0] if failures else {}
            nodeid = first_failure.get("nodeid", "")
            test_file = nodeid.split("::")[0] if "::" in nodeid else nodeid
            if test_file:
                test_path = root / test_file
                try:
                    if test_path.exists():
                        source_context = f"\n--- File: {test_file} ---\n{test_path.read_text(errors='replace')[:3000]}\n"
                except Exception as exc:
                    logger.debug("Could not read test file %s: %s", test_file, exc)

        user_msg = (
            f"Failing tests:\n{failure_text}\n\n"
            f"Relevant source files:\n{source_context or '(none found)'}\n\n"
            "Produce a unified diff to fix these test failures."
        )

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        try:
            response = client.messages.create(
                model=settings.model_sonnet,
                max_tokens=2048,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            diff_text = response.content[0].text.strip()
        except Exception as exc:
            logger.error("Sonnet patch generation failed: %s", exc)
            return SkillResult(context_data=f"LLM call failed: {exc}", is_error=True)

        # Validate with unidiff
        try:
            from unidiff import PatchSet

            patch_set = PatchSet.from_string(diff_text)
            if len(patch_set) == 0:
                raise ValueError("Empty patch — no files changed")
        except Exception as exc:
            logger.warning("unidiff validation failed: %s", exc)
            return SkillResult(
                context_data=f"Generated diff is not valid unified diff format: {exc}\n\nRaw output:\n{diff_text[:500]}",
                is_error=True,
            )

        return SkillResult(context_data=diff_text)
