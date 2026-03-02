"""
Eval base types — EvalCase, EvalResult, AgentEvalSummary, IntegrationEvalResult.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class EvalCase:
    name:         str
    agent_name:   str
    input:        str
    criteria:     list[str]
    judge_prompt: str
    threshold:    int           # 0-10 — must meet or exceed to pass

    @classmethod
    def from_file(cls, path: Path, agent_name: str) -> "EvalCase":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name         = path.stem,
            agent_name   = agent_name,
            input        = data["input"],
            criteria     = data["criteria"],
            judge_prompt = data.get("judge_prompt", "Score 0-10. Does the response meet all criteria?"),
            threshold    = int(data.get("threshold", 7)),
        )


@dataclass
class EvalResult:
    run_id:     str
    agent_name: str
    test_name:  str
    input:      str
    response:   str
    score:      float
    threshold:  int
    passed:     bool
    reasoning:  str
    latency_ms: float
    timestamp:  str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error:      str | None = None

    @property
    def status_emoji(self) -> str:
        if self.error:
            return "💥"
        return "✅" if self.passed else "❌"


@dataclass
class AgentEvalSummary:
    agent_name:   str
    run_id:       str
    avg_score:    float
    pass_rate:    float           # 0.0–1.0
    total_tests:  int
    passed_tests: int
    results:      list[EvalResult]
    timestamp:    str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def passed(self) -> bool:
        return self.pass_rate >= 0.67  # 2/3 tests must pass

    @property
    def status_emoji(self) -> str:
        if self.avg_score >= 8.0:
            return "✅"
        if self.avg_score >= 6.5:
            return "⚠️"
        return "❌"


@dataclass
class IntegrationEvalResult:
    integration: str
    passed:      bool
    latency_ms:  float | None
    error:       str | None
    checked_at:  str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def status_emoji(self) -> str:
        return "✅" if self.passed else "❌"
