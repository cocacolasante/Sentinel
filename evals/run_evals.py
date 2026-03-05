#!/usr/bin/env python3
"""
Brain Eval CLI — run agent quality evals or integration checks manually.

Usage:
  python3 evals/run_evals.py                      # all agent evals
  python3 evals/run_evals.py --agent engineer     # one agent only
  python3 evals/run_evals.py --agent marketing    # marketing agent
  python3 evals/run_evals.py --nightly            # integration checks only
  python3 evals/run_evals.py --slack              # post results to Slack
  python3 evals/run_evals.py --test test_01_write_function  # single test case

Run from the project root:
  cd ~/ai-brain && python3 evals/run_evals.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))


def _print_summary_table(summaries) -> None:
    print()
    print("=" * 60)
    print("  AGENT EVAL RESULTS")
    print("=" * 60)
    for s in summaries:
        delta_info = ""
        bar = "█" * int(s.avg_score) + "░" * (10 - int(s.avg_score))
        print(
            f"  {s.status_emoji}  {s.agent_name:<12} {s.avg_score:>4.1f}/10  [{bar}]  "
            f"{s.passed_tests}/{s.total_tests} passed"
        )
        for r in s.results:
            status = "✅" if r.passed else "❌"
            print(f"       {status} {r.test_name:<35} {r.score}/10  ({r.latency_ms:.0f}ms)")
            if not r.passed:
                print(f"          → {r.reasoning[:100]}")
    print("=" * 60)


def _print_integration_table(results) -> None:
    print()
    print("=" * 60)
    print("  INTEGRATION RELIABILITY RESULTS")
    print("=" * 60)
    for r in results:
        status = "✅ PASS" if r.passed else "❌ FAIL"
        latency = f"{r.latency_ms:.0f}ms" if r.latency_ms else "n/a"
        error = f"  → {r.error}" if r.error else ""
        print(f"  {status}  {r.integration:<20} {latency}{error}")
    print("=" * 60)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Brain Eval Runner")
    parser.add_argument("--agent", help="Run evals for one agent only (engineer, writer, etc.)")
    parser.add_argument("--test", help="Run a single test case by name (e.g. test_01_write_function)")
    parser.add_argument("--nightly", action="store_true", help="Run integration reliability checks")
    parser.add_argument("--slack", action="store_true", help="Post results to Slack after running")
    parser.add_argument("--no-save", action="store_true", help="Skip saving results to Postgres")
    args = parser.parse_args()

    if args.nightly:
        print("Running nightly integration evals...")
        from app.evals.integrations import run_all_integration_evals

        results = await run_all_integration_evals()
        _print_integration_table(results)
        if args.slack:
            from app.evals.reporter import post_scorecard_to_slack

            await post_scorecard_to_slack([], integration_results=results)
        return

    from app.evals.runner import EvalRunner

    runner = EvalRunner()

    if args.agent:
        agent_name = args.agent.lower()
        evals_dir = Path(__file__).parent / "agents" / agent_name
        if not evals_dir.exists():
            print(f"Error: no eval directory for agent '{agent_name}'")
            print(f"Available: {', '.join(d.name for d in (Path(__file__).parent / 'agents').iterdir() if d.is_dir())}")
            sys.exit(1)

        if args.test:
            # Run single test case
            from app.evals.base import EvalCase
            from app.evals.judge import judge_response

            test_file = evals_dir / f"{args.test}.json"
            if not test_file.exists():
                print(f"Error: test file not found: {test_file}")
                sys.exit(1)
            case = EvalCase.from_file(test_file, agent_name)
            summary = await runner.run_agent(agent_name, evals_dir)
            _print_summary_table([summary])
        else:
            print(f"Running evals for agent: {agent_name}")
            summary = await runner.run_agent(agent_name, evals_dir)
            _print_summary_table([summary])
            if args.slack:
                from app.evals.reporter import post_scorecard_to_slack

                await post_scorecard_to_slack([summary])

    else:
        print("Running all agent evals...")
        summaries = await runner.run_all_agents()
        _print_summary_table(summaries)

        if args.slack:
            previous: dict[str, float] = {}
            for s in summaries:
                prev = runner.get_previous_avg(s.agent_name, exclude_run_id=s.run_id)
                if prev is not None:
                    previous[s.agent_name] = prev
            from app.evals.reporter import post_scorecard_to_slack

            await post_scorecard_to_slack(summaries, previous_scores=previous)
            print("\nScorecard posted to Slack.")


if __name__ == "__main__":
    asyncio.run(main())
