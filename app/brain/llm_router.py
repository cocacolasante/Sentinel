"""
LLM Router — selects the best model per task type and builds the system prompt
dynamically from the active Agent personality + TELOS personal context block.

Phase 1: Claude Sonnet (reasoning/code/writing/research/default)
         Claude Haiku  (fast classification)

Phase 2+: GPT-4o (multimodal/fallback) and Gemini Pro (large context/research)
          will be wired in here without touching the rest of the system.
"""

from __future__ import annotations

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.telos.loader import TelosLoader

settings = get_settings()

# ── Default agent prompt (used when no Agent is selected) ─────────────────────
DEFAULT_AGENT_PROMPT = """You are Brain — Anthony's personalized AI assistant built by CSuite Code.

You are highly capable, direct, and efficient. You know Anthony's goals, working style, \
and preferences. You maintain full context across conversations and help with:
- Software engineering, code review, and architecture decisions
- Business strategy and decision-making
- Content creation (scripts, captions, emails, proposals)
- Research and analysis
- Scheduling, task management, and planning

Guidelines:
- Be concise unless depth is explicitly needed
- Think before responding — quality over speed
- Never hallucinate facts; say "I'm not sure" when uncertain
- When given a task that requires an external action (send email, create calendar event, etc.),
  describe what you would do — action execution is handled via the skill system
"""

# ── Model roster (Phase 1 uses Claude only) ────────────────────────────────────
MODEL_MAP: dict[str, tuple[str, int]] = {
    "code":      ("claude-sonnet-4-6", 8096),
    "reasoning": ("claude-sonnet-4-6", 4096),
    "writing":   ("claude-sonnet-4-6", 4096),
    "research":  ("claude-sonnet-4-6", 4096),
    "classify":  ("claude-haiku-4-5-20251001", 512),
    "default":   ("claude-sonnet-4-6", 2048),
}

# Shared TELOS loader (one instance, 5-min cache)
_telos_loader = TelosLoader(
    telos_dir=settings.telos_dir,
    cache_ttl_seconds=settings.telos_cache_ttl_seconds,
)


def get_telos_loader() -> TelosLoader:
    """Return the shared TelosLoader instance."""
    return _telos_loader


class LLMRouter:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def _select_model(self, task_type: str) -> tuple[str, int]:
        """Return (model_id, max_tokens) for the given task type."""
        return MODEL_MAP.get(task_type, MODEL_MAP["default"])

    def _build_system_prompt(self, agent: "Agent | None" = None) -> str:
        """Combine agent personality (or default) with TELOS context block."""
        from app.agents.base import Agent  # avoid circular at module level
        agent_prompt = agent.system_prompt if agent else DEFAULT_AGENT_PROMPT
        telos_block  = _telos_loader.get_block()
        if telos_block:
            return f"{agent_prompt}\n\n{telos_block}"
        return agent_prompt

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def route(
        self,
        message: str,
        history: list[dict] | None = None,
        agent: "Agent | None" = None,
    ) -> str:
        """
        Route a message to the appropriate LLM and return the response text.

        Args:
            message: The user's message (may be augmented with context).
            history: Prior turns in Anthropic message format.
            agent:   Optional Agent personality — drives model, token budget, system prompt.
        """
        if agent:
            model      = agent.preferred_model
            max_tokens = agent.max_tokens
        else:
            model, max_tokens = self._select_model("default")

        system = self._build_system_prompt(agent)

        messages: list[dict] = []
        if history:
            messages.extend(history[-40:])
        messages.append({"role": "user", "content": message})

        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )

        return response.content[0].text
