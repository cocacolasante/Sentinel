"""
LLM Router — selects the best model per task type.

Phase 1: Claude Sonnet (reasoning/code/writing/research/default)
         Claude Haiku  (fast classification)

Phase 2+: GPT-4o (multimodal/fallback) and Gemini Pro (large context/research)
          will be wired in here without touching the rest of the system.
"""

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

settings = get_settings()

# ── System Prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Brain — Anthony's personalized AI assistant built by CSuite Code.

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
  describe what you would do — action execution will be wired in Phase 2 via n8n
"""

# ── Task classification keywords ──────────────────────────────────────────────
TASK_KEYWORDS: dict[str, list[str]] = {
    "code": [
        "code", "debug", "function", "class", "error", "bug", "refactor",
        "python", "javascript", "typescript", "rust", "sql", "script",
        "implement", "build", "fix", "test", "deploy",
    ],
    "reasoning": [
        "analyze", "compare", "decide", "evaluate", "think through",
        "pros and cons", "should i", "recommend", "strategy", "tradeoff",
        "which", "better option",
    ],
    "writing": [
        "write", "draft", "compose", "email", "caption", "content",
        "blog", "post", "script", "proposal", "summary", "rewrite", "edit",
    ],
    "research": [
        "research", "find", "look up", "what is", "explain", "how does",
        "tell me about", "background on", "overview of",
    ],
    "classify": [
        "is this", "categorize", "label", "type of", "what kind",
        "classify", "identify",
    ],
}

# ── Model roster (Phase 1 uses Claude only) ────────────────────────────────────
MODEL_MAP: dict[str, tuple[str, int]] = {
    "code":      ("claude-sonnet-4-6", 8096),
    "reasoning": ("claude-sonnet-4-6", 4096),
    "writing":   ("claude-sonnet-4-6", 4096),
    "research":  ("claude-sonnet-4-6", 4096),
    "classify":  ("claude-haiku-4-5-20251001", 512),
    "default":   ("claude-sonnet-4-6", 2048),
}


class LLMRouter:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def classify_task(self, message: str) -> str:
        """Classify a message into a task type using keyword matching."""
        msg_lower = message.lower()
        for task_type, keywords in TASK_KEYWORDS.items():
            if any(kw in msg_lower for kw in keywords):
                return task_type
        return "default"

    def _select_model(self, task_type: str) -> tuple[str, int]:
        """Return (model_id, max_tokens) for the given task type."""
        return MODEL_MAP.get(task_type, MODEL_MAP["default"])

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def route(self, message: str, history: list[dict] | None = None) -> str:
        """
        Route a message to the appropriate LLM and return the response text.

        Args:
            message: The user's message.
            history:  List of prior turns in Anthropic message format
                      [{"role": "user"|"assistant", "content": str}, ...]
        """
        task_type = self.classify_task(message)
        model, max_tokens = self._select_model(task_type)

        messages: list[dict] = []
        if history:
            # Cap at last 20 turns (40 messages) to stay within context limits
            messages.extend(history[-40:])
        messages.append({"role": "user", "content": message})

        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        return response.content[0].text
