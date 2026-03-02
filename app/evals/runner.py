"""
EvalRunner — loads test cases, calls agents directly (bypassing dispatcher memory),
judges responses, persists results to Postgres.

Agent calls go directly through LLMRouter so evals don't pollute conversation history.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from app.evals.base    import AgentEvalSummary, EvalCase, EvalResult
from app.evals.judge   import judge_response

logger = logging.getLogger(__name__)

# Path to the agent eval test files (relative to project root on server)
_EVALS_DIR = Path(__file__).parent.parent.parent / "evals" / "agents"

# Map eval directory names → agent names in the registry
_AGENT_DIR_MAP: dict[str, str] = {
    "engineer":   "engineer",
    "writer":     "writer",
    "researcher": "researcher",
    "strategist": "strategist",
    "marketing":  "marketing",
}


class EvalRunner:
    def __init__(self) -> None:
        from app.brain.llm_router  import LLMRouter
        from app.agents.registry   import AgentRegistry
        self._llm     = LLMRouter()
        self._agents  = AgentRegistry()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_all_agents(self, run_id: str | None = None) -> list[AgentEvalSummary]:
        """Run all agent evals. Returns one summary per agent."""
        run_id = run_id or str(uuid.uuid4())[:8]
        summaries: list[AgentEvalSummary] = []

        for dir_name, agent_name in _AGENT_DIR_MAP.items():
            agent_dir = _EVALS_DIR / dir_name
            if not agent_dir.exists():
                logger.warning("Eval dir not found: %s", agent_dir)
                continue
            summary = await self.run_agent(agent_name, agent_dir, run_id=run_id)
            summaries.append(summary)
            logger.info(
                "Eval %s | %s | %.1f/10 | %d/%d passed",
                run_id, agent_name, summary.avg_score,
                summary.passed_tests, summary.total_tests,
            )

        await asyncio.to_thread(self._persist_summaries, summaries)
        return summaries

    async def run_agent(
        self,
        agent_name: str,
        agent_dir: Path,
        run_id: str | None = None,
    ) -> AgentEvalSummary:
        """Run all tests for one agent directory."""
        run_id   = run_id or str(uuid.uuid4())[:8]
        cases    = self._load_cases(agent_dir, agent_name)
        results: list[EvalResult] = []

        for case in cases:
            result = await self._run_case(case, run_id)
            results.append(result)

        avg_score    = sum(r.score for r in results) / len(results) if results else 0.0
        passed_count = sum(1 for r in results if r.passed)

        return AgentEvalSummary(
            agent_name   = agent_name,
            run_id       = run_id,
            avg_score    = round(avg_score, 2),
            pass_rate    = round(passed_count / len(results), 2) if results else 0.0,
            total_tests  = len(results),
            passed_tests = passed_count,
            results      = results,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_cases(self, agent_dir: Path, agent_name: str) -> list[EvalCase]:
        cases = []
        for json_file in sorted(agent_dir.glob("test_*.json")):
            try:
                cases.append(EvalCase.from_file(json_file, agent_name))
            except Exception as exc:
                logger.error("Failed to load eval case %s: %s", json_file, exc)
        return cases

    async def _run_case(self, case: EvalCase, run_id: str) -> EvalResult:
        agent = self._agents.get(case.agent_name)

        # Call LLM directly — no memory writes, no hooks, no skill routing
        t0 = time.monotonic()
        response = ""
        error_msg = None
        try:
            response = await asyncio.to_thread(
                self._llm.route,
                case.input,
                None,   # no history
                agent,  # agent persona
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.error("Eval LLM call failed for %s/%s: %s", case.agent_name, case.name, exc)

        latency_ms = round((time.monotonic() - t0) * 1000, 1)

        # Judge the response (or score 0 on error)
        if error_msg:
            verdict = {"score": 0, "passed": False, "reasoning": f"LLM error: {error_msg}"}
        else:
            verdict = await asyncio.to_thread(
                judge_response,
                response,
                case.criteria,
                case.judge_prompt,
                case.threshold,
            )

        return EvalResult(
            run_id     = run_id,
            agent_name = case.agent_name,
            test_name  = case.name,
            input      = case.input[:200],
            response   = response[:500],
            score      = verdict["score"],
            threshold  = case.threshold,
            passed     = verdict["passed"],
            reasoning  = verdict["reasoning"],
            latency_ms = latency_ms,
            error      = error_msg,
        )

    def _persist_summaries(self, summaries: list[AgentEvalSummary]) -> None:
        """Store all results in Postgres for trend tracking."""
        try:
            from app.db import postgres
            for summary in summaries:
                for result in summary.results:
                    postgres.execute(
                        """
                        INSERT INTO eval_results
                            (run_id, agent_name, test_name, score, passed, threshold, reasoning, latency_ms, error)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            result.run_id, result.agent_name, result.test_name,
                            result.score, result.passed, result.threshold,
                            result.reasoning, result.latency_ms, result.error,
                        ),
                    )
        except Exception as exc:
            logger.error("Failed to persist eval results: %s", exc)

    # ── Prior run comparison ──────────────────────────────────────────────────

    def get_previous_avg(self, agent_name: str, exclude_run_id: str) -> float | None:
        """Return average score from the most recent prior run for comparison."""
        try:
            from app.db import postgres
            row = postgres.execute_one(
                """
                SELECT AVG(score) as avg_score
                FROM eval_results
                WHERE agent_name = %s AND run_id != %s
                  AND created_at > NOW() - INTERVAL '14 days'
                """,
                (agent_name, exclude_run_id),
            )
            val = row["avg_score"] if row else None
            return round(float(val), 2) if val is not None else None
        except Exception:
            return None
