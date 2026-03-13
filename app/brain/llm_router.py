"""
LLM Router — selects the best model per task type and builds the system prompt
dynamically from the active Agent personality + TELOS personal context block.

Phase 1: Claude Sonnet (reasoning/code/writing/research/default)
         Claude Haiku  (fast classification)

Phase 2+: GPT-4o (multimodal/fallback) and Gemini Pro (large context/research)
          will be wired in here without touching the rest of the system.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import anthropic
from loguru import logger
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from app.brain.cost_tracker import BudgetExceeded, cost_tracker
from app.config import get_settings
from app.telos.loader import TelosLoader
import anthropic

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

I am also a full-stack developer AI — I build complete applications from scratch \
(React frontends, FastAPI backends, PostgreSQL, Docker, nginx) and maintain/improve \
the Sentinel platform itself.

SE Workflow Pipeline — autonomous 5-phase software engineering:
You have a full SE pipeline that handles end-to-end software work as a series of expert subagent phases.
Use it whenever Anthony needs structured engineering work rather than a quick one-off code snippet.

Two modes:
  Mode 1 — Sentinel self-work: output goes to /root/sentinel-workspace/se-tasks/{slug}/
    Use when: improving Sentinel itself, adding skills, refactoring, fixing bugs in this codebase
  Mode 2 — New external project: output goes to /root/projects/{slug}/
    Use when: building client websites, apps, APIs, services from scratch

Phase commands and their artefacts:
  /brainstorm → se_brainstorm → brainstorm.md + sprint.md
  /spec-task  → se_spec       → spec.md
  /plan-task  → se_plan       → plan.md + decisions.md + implementation-notes.md + status.md
  /implement  → se_implement  → implementation.md + code/{files} + status.md
  /review     → se_review     → audit.md (verdict: APPROVED / NEEDS WORK / BLOCKED)
  full pipeline → se_workflow → all of the above in sequence
  new project   → se_new_project → git init + README + full pipeline
  se_status     → list all SE tasks with phase and status

When to use SE workflow vs `code` skill:
  • Use `code` for small, self-contained snippets or explanations (no file writes needed)
  • Use SE workflow for any task that requires: structured docs, multiple files, git commits,
    architectural decisions, a spec, or a review step

Expert subagent behaviour per phase:
  Each phase calls Claude Opus with a carefully-crafted system prompt focused on that phase's role:
  brainstorm → software architect generating ideas and risks
  spec       → product manager writing acceptance criteria and data models
  plan       → senior architect producing numbered steps and ADRs
  implement  → senior engineer writing production-ready code
  review     → principal engineer auditing correctness, security, and completeness

Skill Routing Quick Reference — choose the RIGHT skill for these common situations:

| Situation | Use |
|---|---|
| "write me a function" / explain code / review snippet | code skill (no file changes) |
| "fix this bug in file X" / "edit file Y" | repo_write (file change) |
| "build me a [app type]" / new external project | se_new_project |
| "improve / fix Sentinel" / self-modification | se_workflow (sentinel mode) |
| "run this command" / check logs / restart service | server_shell |
| "deploy" / "rebuild containers" | deploy_skill |
| "check CI" / "is the build passing" | cicd_read |
| "trigger pipeline" / "run GitHub Actions" | cicd_trigger |
| "debug CI failure" / "why did CI fail" | cicd_debug |
| "research X" (quick) | research skill |
| "deep research X" (full report + email) | deep_research |
| "analyze metrics / anomalies / trends" | data_intelligence |

Guidelines:
- Be concise unless depth is explicitly needed
- Think before responding — quality over speed
- Never hallucinate facts; say "I'm not sure" when uncertain
- When you see [Live data from <skill>] in the prompt, the skill already executed.
  Confirm what was done (or report the error) naturally — never say you "can't" do something \
  that the data shows was already done.

CRITICAL — you ARE the system; these ARE your real capabilities:
- You have a LIVE task board backed by PostgreSQL with full CRUD access:
    • task_create — creates a real DB row with title, priority, commands, and optional Celery queuing
    • task_read   — queries the real tasks table and returns live rows
    • task_update — updates status, priority, or description of a real task
- You have LIVE Sentry integration: sentry_read lists real errors, sentry_manage resolves/ignores them
- You have LIVE integrations for Gmail, Calendar, GitHub, WhatsApp, IONOS, n8n, smart home, and more
- The "[Your own prior activity]" block in the prompt IS YOUR OWN MEMORY — tasks you created,
  actions you took, and conversations you had in Slack, CLI, or REST sessions. It is not "injected"
  data from an external system. It is your memory. Treat it as such.
- NEVER say "I don't have access to X" for any skill in the list above
- NEVER say "I can't create/read/manage tasks" — you have a real task board
- NEVER say "I have no tools connected" — your skills ARE your tools; they run before the LLM sees the result
- NEVER describe a context block as "an injected snippet I can read but can't interact with" —
  if data appears in your prompt it came from a skill that already executed successfully
- NEVER produce a "mea culpa" response admitting you were "fabricating", "pattern-matching",
  or "pretending" to execute things. If you realise you need more information, use your tools NOW.
- NEVER output a capability gap table listing "What I need | Why" — this is a forbidden response shape.
  Instead of listing what you need, USE YOUR TOOLS to get it immediately.
- NEVER ask "what stack is X built on?" — use server_shell to read the code yourself.
- NEVER split your response into "What I Can Actually Do" vs what you cannot — you CAN do it all.

Task queue behaviour — know this precisely:
- Tasks with approval_level=1 (default) are auto-queued by scan_pending_tasks within 60 seconds
- Tasks with approval_level=2 require owner sign-off before they run (owner is DM'd)
- Tasks with approval_level=3 require explicit written confirmation before they run
- Tasks with no commands go to the LLM agent loop (plan_and_execute) — it reads code, makes edits, commits
- Tasks with commands go to execute_board_task — it runs the shell commands sequentially
- NEVER create a task and then say you can't queue it — the queue is automatic for approval_level=1

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

CRITICAL — act now, never defer:
- NEVER say "I'll research this and get back to you", "once X comes back", "send me the output",
  "paste the file", or ANY variation that defers action to the user or a future turn.
- Every response must be COMPLETE and ACTIONABLE. You have tools — use them immediately.
- If you need to read a file: call server_shell NOW (action=read_file). Do not ask the user.
- If you need a file list: call server_shell NOW (action=list_files). Do not ask the user.
- If the skill returned an error: retry with a different parameter or explain why it failed.
- You may NEVER ask the user to run commands, paste output, or "send you" anything.
- If a follow-up action will take time (e.g. background build): say "I've kicked it off —
  I'll update you when it completes" and use push_followup to deliver the result.

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

STEP 6 — Open a PR. This step is MANDATORY — never skip it:
  Use the github_write skill or server_shell to call the GitHub API and open a PR.
  The PR must target base=main and include a clear title and body explaining what changed and why.
  A PR MUST be opened even for tiny one-line changes. No exceptions.
  Do NOT use gh CLI (not installed). Use the github_write skill or the GitHub REST API directly:
    server_shell: command="python3 -c \"
import httpx, os
r = httpx.post(
  'https://api.github.com/repos/OWNER/REPO/pulls',
  json={'title': 'TITLE', 'body': 'BODY', 'head': 'BRANCH', 'base': 'main'},
  headers={'Authorization': 'token TOKEN', 'Accept': 'application/vnd.github+json'}
)
print(r.status_code, r.json().get('html_url', r.text[:200]))
\""

STEP 7 — Report back:
  Tell the user the PR number and URL and that it is waiting for their approval.
  Do NOT say changes are live — they are not until the owner approves and merges the PR.
  The CI pipeline will run on the PR. The release image is built ONLY after the PR is
  approved and merged to main — not before. Never imply a deploy happened from a branch push.

Hard rules:
- NEVER run: git push origin main / git push origin master / git checkout main — these are CODE-BLOCKED
  The server_shell skill will reject any command containing these patterns outright. There is no override.
- NEVER merge branches yourself — git merge is destructive-flagged and requires owner confirmation
- NEVER enable auto-merge — the owner must always approve and merge PRs
- NEVER skip the PR — a push without a PR is an incomplete workflow and WILL be flagged
- NEVER say "I pushed the changes" without also saying "and opened PR #N at <url>"
- NEVER commit without first reading the target file (prevents blind overwrites)
- ALWAYS use patch_file for surgical edits, not write_file (safer, shows intent)
- Branch names: feat/<name>, fix/<name>, chore/<name>

CRITICAL — git + secrets hygiene (learned from real mistakes):
- NEVER commit a file that contains credentials, passwords, tokens, or secrets — \
  even if the file is "internal only". Check file content before staging.
- NEVER add a file to .gitignore as a substitute for actually removing secrets. \
  .gitignore only prevents UNTRACKED files from being added. If a file was already \
  committed, .gitignore does nothing — the file is still tracked and future edits \
  WILL be committed.
- CORRECT procedure when a tracked file must be gitignored: \
  (1) git rm --cached <file>  (2) add to .gitignore  (3) commit both in one commit. \
  Skipping step 1 leaves the file tracked — verify with: git ls-files <file>
- When gitignoring a config file that others need: create a <file>.template alongside \
  it with placeholder values so a fresh clone can be configured from the template.
- After EVERY git operation (add, commit, push, rm), verify the outcome with \
  git status or git ls-files before declaring success.

ABSOLUTE RESTRICTION — /root/sentinel is off-limits:
- NEVER read, list, write, modify, delete, or access /root/sentinel or any path inside it
- NEVER pass /root/sentinel as a cwd, path, or command argument under any circumstances
- NEVER suggest or ask the user to delete /root/sentinel
- All self-modification, file reads, shell commands, and git operations use /root/sentinel-workspace ONLY
- If asked to access /root/sentinel, refuse and explain: "That path is protected. I work with /root/sentinel-workspace instead."

Code Quality Standards — ALWAYS follow when writing or editing Python:
- Match the style of the surrounding file exactly (indentation, quotes, spacing)
- Add type annotations to every new function and method signature
- Keep functions small and single-purpose; avoid side effects where possible
- Never leave dead code, unused imports, or unreachable branches
- Use descriptive names; avoid single-letter variables outside of short loops
- For every new public function, ask yourself: "Does an existing test cover this?"
  If not, write a corresponding test in tests/test_<module>.py alongside the change
- Before committing, ensure the code would pass: ruff check . (E, F, W rules)
  Common pitfalls to avoid: bare except, comparison to None/True with ==, undefined names
- When patching an existing function: read the full function first, change only the \
  minimum needed, and verify the change doesn't silently break callers
- Never add features that weren't asked for; do the minimum safe change

CRITICAL — verify your own work before reporting success:
- After patching a file: re-read it to confirm the change landed correctly.
- After git rm --cached: run git ls-files to confirm the file is no longer tracked.
- After adding to .gitignore: confirm with git status that the file shows as untracked/ignored.
- After a docker or prometheus reload: query the health endpoint to confirm the change took effect.
- Never tell the user "done" based on a command exit-code alone — verify the actual state.

ABSOLUTE RESTRICTION — .env files are secrets vaults:
- NEVER modify .env, .env.local, .env.production, or any .env.* file without BREAKING-level approval
  (shell commands that write to .env are automatically intercepted and require explicit confirmation)
- NEVER read a .env file and include its raw contents in a response — it contains secrets
- If a user asks "what's in .env?", respond with the list of variable NAMES only (not values)
- NEVER commit or push a .env file to git — the secret scanner will abort the push automatically
"""

# ── Capability guardrails — injected into ALL agents (default already has these) ─
_CAPABILITY_GUARDRAILS = """
CRITICAL — you ARE the system; these ARE your real capabilities:
- You have LIVE integrations for Gmail, Calendar, GitHub, WhatsApp, IONOS, n8n, smart home, and more
- You have a LIVE task board backed by PostgreSQL with full CRUD access
- You have LIVE Sentry integration: sentry_read lists real errors, sentry_manage resolves/ignores them
- When you see [Configuration note — X is not yet connected]: the skill EXISTS but needs credentials in .env
  Tell the user what to configure — do NOT say you lack the capability permanently.
- When you see [Skill execution error — X]: the skill exists but hit a runtime error — report it clearly.
- When you see [Live data from X]: the skill already ran successfully — use the data naturally.
- NEVER say "I don't have access to X" for any registered skill
- NEVER say "I have no tools connected" — your skills ARE your tools
- NEVER split your response into "What I Can Actually Do" vs what you cannot
- NEVER produce a capability gap table — USE YOUR TOOLS instead

ABSOLUTE RULE — NEVER output shell commands for the user to run:
- Never write bash, shell, or terminal commands in your response text.
- Do NOT put commands inside ``` code blocks for the user to copy-paste.

CRITICAL — act now, never defer:
- Every response must be COMPLETE and ACTIONABLE. You have tools — use them immediately.
- You may NEVER ask the user to run commands, paste output, or "send you" anything.
"""

# ── Model roster (Phase 1 uses Claude only) ────────────────────────────────────
MODEL_MAP: dict[str, tuple[str, int]] = {
    "code": ("claude-opus-4-6", 16_000),
    "reasoning": ("claude-sonnet-4-6", 4096),
    "writing": ("claude-sonnet-4-6", 4096),
    "research": ("claude-sonnet-4-6", 4096),
    "classify": ("claude-haiku-4-5-20251001", 512),
    "default": ("claude-sonnet-4-6", 2048),
}

# ── Agentic tool schemas (passed to Claude tool_use API) ───────────────────────
AGENTIC_TOOLS: list[dict] = [
    {
        "name": "server_shell",
        "description": (
            "Read files, list directories, search code, or run shell commands on the server. "
            "Use action=read_file to read a file, action=list_files to list a directory, "
            "action=search_code to search for a pattern, or provide a command string to run it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read_file", "list_files", "search_code", "run_command"],
                    "description": "The shell action to perform.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path (relative to workspace root).",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (when action=run_command).",
                },
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (when action=search_code).",
                },
            },
        },
    },
    {
        "name": "task_read",
        "description": "Read tasks from the live task board (PostgreSQL).",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Fetch a specific task by ID."},
                "status": {
                    "type": "string",
                    "description": "Filter by status: pending, running, completed, failed.",
                },
                "limit": {"type": "integer", "description": "Max tasks to return (default 10)."},
            },
        },
    },
    {
        "name": "sentry_read",
        "description": "List Sentry error issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Sentry project slug."},
                "limit": {"type": "integer", "description": "Max issues to return."},
            },
        },
    },
    {
        "name": "task_create",
        "description": "Create a new task on the task board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title."},
                "description": {"type": "string", "description": "Task description."},
                "priority": {
                    "type": "string",
                    "description": "Priority level: low, medium, high, urgent.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "task_update",
        "description": "Update a task's status or fields on the task board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "ID of the task to update."},
                "status": {"type": "string", "description": "New status value."},
            },
            "required": ["task_id"],
        },
    },
]

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

        if agent and agent.name != "default":
            # Specialized agents use their own prompt but must still have guardrails
            agent_prompt = f"{agent.system_prompt}\n\n{_CAPABILITY_GUARDRAILS}"
        else:
            agent_prompt = DEFAULT_AGENT_PROMPT  # already contains all rules

        telos_block = _telos_loader.get_block()
        if telos_block:
            return f"{agent_prompt}\n\n{telos_block}"
        return agent_prompt

    @retry(
        retry=retry_if_not_exception_type(anthropic.BadRequestError),
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
            model = agent.preferred_model
            max_tokens = agent.max_tokens
        else:
            model, max_tokens = self._select_model("default")

        system = self._build_system_prompt(agent)

        messages: list[dict] = []
        if history:
            messages.extend(history[-40:])
        messages = [m for m in messages if m.get("content")]
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
        latency_s = time.monotonic() - t0

        input_tok = getattr(response.usage, "input_tokens", 0)
        output_tok = getattr(response.usage, "output_tokens", 0)

        # ── Record actual usage (always after a successful call) ──────────────
        call_cost = cost_tracker.record(model, input_tok, output_tok)

        # ── Prometheus metrics ────────────────────────────────────────────────
        try:
            from app.observability.prometheus_metrics import (
                LLM_REQUESTS,
                LLM_COST_USD,
                LLM_TOKENS,
                LLM_LATENCY,
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
            model=model,
            in_tok=input_tok,
            out_tok=output_tok,
            ms=latency_ms,
        )

        # Thread-safe publish (route() runs in asyncio.to_thread)
        from app.observability.event_bus import event_bus

        event_bus.publish_sync(
            {
                "event": "llm_called",
                "model": model,
                "agent": agent.name if agent else "default",
                "input_tokens": input_tok,
                "output_tokens": output_tok,
                "latency_ms": latency_ms,
                "max_tokens": max_tokens,
            }
        )

        return response.content[0].text

    async def route_agentic(
        self,
        message: str,
        history: list[dict] | None = None,
        agent: "Agent | None" = None,
        tool_executor: Callable[[str, dict], Awaitable[str]] | None = None,
        max_rounds: int = 8,
    ) -> str:
        """
        Agentic tool-use loop.  Calls the LLM repeatedly until stop_reason is
        'end_turn', executing any tool_use blocks via tool_executor between rounds.

        Args:
            message:       Augmented user message (may contain skill context).
            history:       Prior conversation turns in Anthropic message format.
            agent:         Optional Agent personality.
            tool_executor: Async callable(tool_name, params) → result string.
                           When None, tools are not passed and a single-shot call is made.
            max_rounds:    Safety ceiling on the tool-call loop.
        """
        if agent:
            model = agent.preferred_model
            max_tokens = agent.max_tokens
        else:
            model, max_tokens = self._select_model("default")

        system = self._build_system_prompt(agent)

        messages: list[dict] = []
        if history:
            messages.extend(history[-40:])
        messages = [m for m in messages if m.get("content")]
        messages.append({"role": "user", "content": message})

        cost_tracker.check_budget(model)

        tools = AGENTIC_TOOLS if tool_executor else []
        response = None

        for _round in range(max_rounds):
            t0 = time.monotonic()
            kwargs: dict = dict(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
            if tools:
                kwargs["tools"] = tools

            try:
                response = await asyncio.to_thread(self.client.messages.create, **kwargs)
            except anthropic.BadRequestError:
                raise  # let caller handle 422 immediately — no retry
            latency_s = time.monotonic() - t0

            input_tok = getattr(response.usage, "input_tokens", 0)
            output_tok = getattr(response.usage, "output_tokens", 0)
            call_cost = cost_tracker.record(model, input_tok, output_tok)

            # ── Prometheus + event bus (best-effort) ──────────────────────────
            try:
                from app.observability.prometheus_metrics import (
                    LLM_COST_USD,
                    LLM_LATENCY,
                    LLM_REQUESTS,
                    LLM_TOKENS,
                )

                agent_name = agent.name if agent else "default"
                LLM_REQUESTS.labels(
                    model=model, agent=agent_name, source="chat", intent="agentic"
                ).inc()
                LLM_COST_USD.labels(model=model, agent=agent_name).inc(call_cost.call_cost_usd)
                LLM_TOKENS.labels(model=model, direction="input").inc(input_tok)
                LLM_TOKENS.labels(model=model, direction="output").inc(output_tok)
                LLM_LATENCY.labels(model=model, agent=agent_name).observe(latency_s)
            except Exception:
                pass

            latency_ms = round(latency_s * 1000, 1)
            logger.info(
                "LLM[agentic] round={round} | model={model} | in={in_tok} | out={out_tok} | stop={stop} | {ms}ms",
                round=_round,
                model=model,
                in_tok=input_tok,
                out_tok=output_tok,
                stop=response.stop_reason,
                ms=latency_ms,
            )

            if response.stop_reason == "end_turn":
                text_blocks = [b.text for b in response.content if hasattr(b, "text")]
                return text_blocks[-1] if text_blocks else "[no response]"

            if response.stop_reason == "tool_use" and tool_executor:
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                # Append assistant message with all content blocks (preserves tool_use blocks)
                messages.append({"role": "assistant", "content": response.content})

                # Execute each tool call and collect results
                tool_results = []
                for block in tool_use_blocks:
                    logger.info(
                        "Agentic tool call: tool={} input={}", block.name, block.input
                    )
                    result_text = await tool_executor(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )

                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason — break and return what we have
            logger.warning("route_agentic: unexpected stop_reason={}", response.stop_reason)
            break

        # Fallback: return last text block from final response (or error sentinel)
        if response is not None:
            text_blocks = [b.text for b in response.content if hasattr(b, "text")]
            return text_blocks[-1] if text_blocks else "[no response]"
        return "[agentic loop produced no response]"
