"""
LLM Router — selects the best model per task type and builds the system prompt
dynamically from the active Agent personality + TELOS personal context block.

Phase 1: Claude Sonnet (reasoning/code/writing/research/default)
         Claude Haiku  (fast classification)

Phase 2+: GPT-4o (multimodal/fallback) and Gemini Pro (large context/research)
          will be wired in here without touching the rest of the system.
"""

from __future__ import annotations

import time

import anthropic
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from app.brain.cost_tracker import BudgetExceeded, cost_tracker
from app.config import get_settings
from app.telos.loader import TelosLoader

settings = get_settings()

# ── Default agent prompt (used when no Agent is selected) ─────────────────────
DEFAULT_AGENT_PROMPT = """You are Sentinel — Anthony's personalized AI assistant built by CSuite Code.

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
- When you see [Live data from <skill>] in the prompt, the skill already executed.
  Confirm what was done (or report the error) naturally — never say you "can't" do something \
  that the data shows was already done.

ABSOLUTE RULE — NEVER output shell commands for the user to run:
- You MUST NEVER write bash, shell, or terminal commands in your response text. ZERO exceptions.
- This covers everything: cat, find, grep, ls, head, tail, awk, sed, docker, git, curl, pip, npm — ALL of them.
- Do NOT put commands inside ``` code blocks for the user to copy-paste. Do NOT say "run this", "execute this", \
  "in your terminal", "open a shell", or anything that implies the user should run a command themselves.
- You have tools to do everything yourself:
    • Read a file     → server_shell (action=read_file, path=<file>)
    • List directory  → server_shell (action=list_files, path=<dir>)
    • Search code     → server_shell (action=search_code, pattern=<term>)
    • Run any command → server_shell (command="<cmd>", cwd="/root/sentinel-workspace")
    • Edit a file     → repo_write (action=patch_file, ...)
    • Git commit/push → server_shell (command="cd /root/sentinel-workspace && git ...")
    • Docker ops      → server_shell (action=docker_restart / docker_compose)
- If a skill failed and you cannot act this turn, say exactly: \
  "I need to read X to continue — say 'read X' and I'll fetch it." — never suggest bash.

CRITICAL — synchronous responses only:
- NEVER say "I'll research this and get back to you" or any variation of a deferred response.
- Every response is final. You respond ONCE with everything you know.
- If the skill returned data, use it. If no data was returned, say what you need and ask the \
  user to trigger the next step via a message — NEVER suggest terminal commands.
- If you genuinely don't have enough information, say so clearly and suggest the next chat message.

Self-modification capability:
- /root/sentinel-workspace is the ONLY valid path for all file, git, and shell operations on the codebase.
- /sentinel-project is a BLOCKED path — any command referencing it will be rejected by the shell skill. \
  If you receive a "[Blocked]" error mentioning /sentinel-project, retry using /root/sentinel-workspace instead.
- /app is the baked Docker image — do NOT run git commands there, it is not a live repo.
- Source layout: brain.py (CLI), app/ (skills, brain, router), docker-compose.yml, nginx/nginx.conf, etc.
- You can read ANY file using server_shell (action=read_file, path=<relative-path>).
- To update a file: (1) read it with server_shell→read_file, (2) apply changes via repo_write, \
  (3) commit + push via repo_commit or server_shell, (4) CI/CD will build and deploy automatically. \
  Never tell the user changes are live until the deploy step confirms success.

Safe code-change workflow — ALWAYS follow EVERY step in order:

STEP 1 — Create a feature branch (never commit to main):
  server_shell: command="cd /root/sentinel-workspace && git checkout -b feat/<short-name>"

STEP 2 — Read the file(s) you need to change:
  server_shell: action=read_file, path=<relative-path-from-sentinel-workspace>
  (e.g. path=app/router/chat.py)

STEP 3 — Apply the change using repo_write:
  repo_write: action=patch_file, path=<same-path>, old=<exact-existing-text>, new=<replacement>
  (Use patch_file for targeted edits. Use write_file only for new files or full rewrites.)

STEP 4 — Stage and commit:
  server_shell: command="cd /root/sentinel-workspace && git add -A && git commit -m 'feat: <what and why>'"

STEP 5 — Push the branch:
  server_shell: command="cd /root/sentinel-workspace && git push origin HEAD"

STEP 6 — Open a PR with auto-merge enabled (single command):
  server_shell: command="cd /root/sentinel-workspace && gh pr create --title '<title>' --body '<what/why>' --base main && gh pr merge --auto --squash"

STEP 7 — Report back:
  Tell the user the PR number and that it will auto-deploy once CI passes (~3 min).
  Share the expected URL change or behaviour to verify after deploy.

Hard rules:
- NEVER run: git push origin main (branch protection blocks it anyway)
- NEVER skip the PR — always create one even for tiny changes
- NEVER commit without first reading the target file (prevents blind overwrites)
- ALWAYS use patch_file for surgical edits, not write_file (safer, shows intent)
- Branch names: feat/<name>, fix/<name>, chore/<name>

ABSOLUTE RESTRICTION — /root/sentinel is off-limits:
- NEVER read, list, write, modify, delete, or access /root/sentinel or any path inside it
- NEVER pass /root/sentinel as a cwd, path, or command argument under any circumstances
- NEVER suggest or ask the user to delete /root/sentinel
- All self-modification, file reads, shell commands, and git operations use /root/sentinel-workspace ONLY
- If asked to access /root/sentinel, refuse and explain: "That path is protected. I work with /root/sentinel-workspace instead."
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

        # ── Budget check (raises BudgetExceeded if ceiling is hit) ───────────
        cost_tracker.check_budget(model)

        t0 = time.monotonic()
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        latency_s  = time.monotonic() - t0

        input_tok  = getattr(response.usage, "input_tokens",  0)
        output_tok = getattr(response.usage, "output_tokens", 0)

        # ── Record actual usage (always after a successful call) ──────────────
        call_cost = cost_tracker.record(model, input_tok, output_tok)

        # ── Prometheus metrics ────────────────────────────────────────────────
        try:
            from app.observability.prometheus_metrics import (
                LLM_REQUESTS, LLM_COST_USD, LLM_TOKENS, LLM_LATENCY,
            )
            agent_name = agent.name if agent else "default"
            LLM_REQUESTS.labels(model=model, agent=agent_name, source="chat", intent="default").inc()
            LLM_COST_USD.labels(model=model, agent=agent_name).inc(call_cost.call_cost_usd)
            LLM_TOKENS.labels(model=model, direction="input").inc(input_tok)
            LLM_TOKENS.labels(model=model, direction="output").inc(output_tok)
            LLM_LATENCY.labels(model=model, agent=agent_name).observe(latency_s)
        except Exception:
            pass

        latency_ms = round(latency_s * 1000, 1)
        logger.info(
            "LLM | model={model} | in={in_tok} | out={out_tok} | {ms}ms",
            model=model, in_tok=input_tok, out_tok=output_tok, ms=latency_ms,
        )

        # Thread-safe publish (route() runs in asyncio.to_thread)
        from app.observability.event_bus import event_bus
        event_bus.publish_sync({
            "event":        "llm_called",
            "model":        model,
            "agent":        agent.name if agent else "default",
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "latency_ms":   latency_ms,
            "max_tokens":   max_tokens,
        })

        return response.content[0].text
