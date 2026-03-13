"""
TestGeneratorSkill — parse a source file with AST, extract public functions/methods,
call Claude Sonnet to generate pytest tests, and write the test file.

Intent: generate_tests
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import anthropic

from app.config import get_settings
from app.skills.base import BaseSkill, SkillResult
from app.utils.shell import run as shell_run

logger = logging.getLogger(__name__)
settings = get_settings()

_SYSTEM = (
    "You are an expert Python test engineer. Given a source file and its public symbols, "
    "generate a comprehensive pytest test file.\n\n"
    "Requirements:\n"
    "- Use pytest fixtures and parametrize where appropriate\n"
    "- Mock external dependencies (database, HTTP, LLM APIs)\n"
    "- Test happy path and common error cases\n"
    "- Return ONLY the Python test file content — no prose, no markdown fences"
)


def _extract_symbols(source: str) -> list[dict]:
    """Extract public functions/methods/classes from source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    symbols: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            args = [a.arg for a in node.args.args if a.arg != "self"]
            docstring = ast.get_docstring(node) or ""
            symbols.append(
                {
                    "type": "function",
                    "name": node.name,
                    "args": args,
                    "docstring": docstring[:200],
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                }
            )
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            methods = []
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not child.name.startswith("_") or child.name in ("__init__",):
                        methods.append(child.name)
            symbols.append({"type": "class", "name": node.name, "methods": methods[:10]})

    return symbols


class TestGeneratorSkill(BaseSkill):
    name = "test_generator"
    description = (
        "Generate pytest tests for a Python source file using AST analysis and Claude Sonnet. "
        "Use for: 'generate tests for X', 'write tests for Y file', 'create unit tests'."
    )
    trigger_intents = ["generate_tests"]

    def is_available(self) -> bool:
        return bool(settings.anthropic_api_key)

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        source_path: str = params.get("source_path", "")
        repo_path: str = params.get("repo_path", "/root/sentinel-workspace")

        if not source_path:
            return SkillResult(context_data="No source_path provided.", is_error=True)

        root = Path(repo_path)
        full_path = root / source_path if not Path(source_path).is_absolute() else Path(source_path)

        if not full_path.exists():
            return SkillResult(context_data=f"File not found: {full_path}", is_error=True)

        source = full_path.read_text(errors="replace")
        symbols = _extract_symbols(source)

        if not symbols:
            return SkillResult(context_data=f"No public symbols found in {source_path}")

        module_name = full_path.stem
        symbol_summary = json.dumps(symbols, indent=2)

        user_msg = (
            f"Source file: {source_path}\n\n"
            f"Public symbols:\n{symbol_summary}\n\n"
            f"Full source:\n{source[:4000]}\n\n"
            f"Generate a comprehensive pytest test file for this module."
        )

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        try:
            response = client.messages.create(
                model=settings.model_sonnet,
                max_tokens=4096,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            test_content = response.content[0].text.strip()
        except Exception as exc:
            logger.error("Sonnet test generation failed: %s", exc)
            return SkillResult(context_data=f"LLM call failed: {exc}", is_error=True)

        # Strip markdown code blocks if present
        if test_content.startswith("```"):
            lines = test_content.splitlines()
            test_content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        # Write test file
        tests_dir = root / "tests"
        tests_dir.mkdir(exist_ok=True)
        output_path = tests_dir / f"test_{module_name}_generated.py"
        output_path.write_text(test_content)

        # Verify tests collect (no import errors) via quick dry-run
        collect_ok = False
        try:
            result = await shell_run(
                ["pytest", str(output_path), "--collect-only", "-q"],
                cwd=str(root),
                timeout=30,
            )
            collect_ok = result.ok
        except Exception as exc:
            logger.debug("collect-only check failed: %s", exc)

        n_symbols = len([s for s in symbols if s["type"] == "function"])
        status = "✅ collects cleanly" if collect_ok else "⚠️ may have import errors"
        return SkillResult(
            context_data=(
                f"Generated {n_symbols} test(s) in `tests/test_{module_name}_generated.py` — {status}"
            )
        )
