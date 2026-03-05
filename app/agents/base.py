"""Agent dataclass — defines a named AI persona with model and prompt preferences."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Agent:
    name: str
    display_name: str
    system_prompt: str
    preferred_model: str = "claude-sonnet-4-6"
    max_tokens: int = 2048
    temperature: float = 1.0
    trigger_intents: list[str] = field(default_factory=list)
    trigger_keywords: list[str] = field(default_factory=list)
