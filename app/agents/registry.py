"""
AgentRegistry — selects the best agent persona for each request.

Selection priority:
  1. Intent match (trigger_intents)
  2. Keyword scan (trigger_keywords, case-insensitive)
  3. Default agent
"""

from __future__ import annotations

import logging

from app.agents.base import Agent

logger = logging.getLogger(__name__)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: list[Agent] = []
        self._by_name: dict[str, Agent] = {}
        self._load_defaults()

    def _load_defaults(self) -> None:
        from app.agents.definitions import (
            ENGINEER_AGENT,
            WRITER_AGENT,
            RESEARCHER_AGENT,
            STRATEGIST_AGENT,
            MARKETING_AGENT,
            DEFAULT_AGENT,
        )

        for agent in [
            ENGINEER_AGENT,
            WRITER_AGENT,
            RESEARCHER_AGENT,
            STRATEGIST_AGENT,
            MARKETING_AGENT,
            DEFAULT_AGENT,
        ]:
            self.register(agent)

    def register(self, agent: Agent) -> None:
        self._agents.append(agent)
        self._by_name[agent.name] = agent

    def select(self, intent: str, message: str) -> Agent:
        """Return the best matching agent for intent + message content."""
        from app.agents.definitions import DEFAULT_AGENT

        # 1. Intent match
        for agent in self._agents:
            if intent in agent.trigger_intents:
                logger.debug("Agent selected by intent: %s → %s", intent, agent.name)
                return agent

        # 2. Keyword scan
        msg_lower = message.lower()
        for agent in self._agents:
            if any(kw in msg_lower for kw in agent.trigger_keywords):
                logger.debug("Agent selected by keyword: %s", agent.name)
                return agent

        return DEFAULT_AGENT

    def get(self, name: str) -> Agent | None:
        return self._by_name.get(name)

    def list_agents(self) -> list[dict]:
        return [
            {
                "name": a.name,
                "display_name": a.display_name,
                "preferred_model": a.preferred_model,
                "max_tokens": a.max_tokens,
                "trigger_intents": a.trigger_intents,
            }
            for a in self._agents
        ]
