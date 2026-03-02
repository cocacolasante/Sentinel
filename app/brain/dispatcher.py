"""
Brain Dispatcher — the central orchestrator.

Flow for each incoming message:
  1. Fire PRE_PROCESS hooks (security check, logging)
  2. Check Redis for a pending write-action awaiting confirmation
  3. If confirming/cancelling → execute or abort pending action
  4. Otherwise classify intent via IntentClassifier (Haiku, registry-driven)
  5. Select Agent personality for the classified intent
  6. Dispatch to SkillRegistry → execute skill → SkillResult
  7. Augment the message with context → call LLM router (with agent)
  8. Persist turn to MemoryManager (Redis hot + Postgres flush + Qdrant)
  9. Fire POST_PROCESS hooks (logging)
  10. Return DispatchResult to caller (REST or Slack)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from loguru import logger

from app.brain.cost_tracker import BudgetExceeded
from app.brain.intent       import IntentClassifier
from app.brain.llm_router   import LLMRouter
from app.brain.rate_limiter import RateLimitExceeded, rate_limiter
from app.config             import get_settings
from app.observability.event_bus import event_bus

settings = get_settings()

# ── Confirmation trigger words ────────────────────────────────────────────────
_CONFIRM_WORDS = {"confirm", "send", "yes", "do it", "proceed", "go ahead", "send it"}
_CANCEL_WORDS  = {"cancel", "no", "stop", "abort", "nevermind", "never mind", "don't"}

PENDING_TTL = 300  # 5 minutes


def _capture_error(exc: Exception, context: dict | None = None) -> None:
    """Send exception to Sentry if configured, otherwise log it."""
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            if context:
                for k, v in context.items():
                    scope.set_extra(k, v)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass  # Sentry not installed or not configured — already logged by caller


@dataclass
class DispatchResult:
    reply:      str
    intent:     str
    session_id: str
    agent:      str = "default"


def _build_skill_registry():
    """Construct and return a fully-populated SkillRegistry."""
    from app.skills.registry        import SkillRegistry
    from app.skills.chat_skill      import ChatSkill
    from app.skills.gmail_skill     import GmailReadSkill, GmailSendSkill
    from app.skills.calendar_skill  import CalendarReadSkill, CalendarWriteSkill
    from app.skills.github_skill    import GitHubReadSkill, GitHubWriteSkill
    from app.skills.smart_home_skill import SmartHomeSkill
    from app.skills.n8n_skill       import N8nSkill
    from app.skills.research_skill          import ResearchSkill
    from app.skills.code_skill             import CodeSkill
    from app.skills.content_draft_skill    import ContentDraftSkill
    from app.skills.social_caption_skill   import SocialCaptionSkill
    from app.skills.ad_copy_skill          import AdCopySkill
    from app.skills.content_repurpose_skill import ContentRepurposeSkill
    from app.skills.content_calendar_skill import ContentCalendarSkill

    reg = SkillRegistry()
    reg.register(ChatSkill())
    reg.register(GmailReadSkill())
    reg.register(GmailSendSkill())
    reg.register(CalendarReadSkill())
    reg.register(CalendarWriteSkill())
    reg.register(GitHubReadSkill())
    reg.register(GitHubWriteSkill())
    reg.register(SmartHomeSkill())
    reg.register(N8nSkill())
    reg.register(ResearchSkill())
    reg.register(CodeSkill())
    reg.register(ContentDraftSkill())
    reg.register(SocialCaptionSkill())
    reg.register(AdCopySkill())
    reg.register(ContentRepurposeSkill())
    reg.register(ContentCalendarSkill())
    return reg


def _build_hook_registry():
    """Construct and return a fully-populated HookRegistry."""
    from app.hooks.registry       import HookRegistry
    from app.hooks.security_hook  import SecurityHook
    from app.hooks.logging_hook   import LoggingHook
    from app.hooks.session_hook   import SessionHook

    reg = HookRegistry()
    reg.register(SecurityHook())
    reg.register(LoggingHook())
    reg.register(SessionHook())
    return reg


class Dispatcher:
    def __init__(self) -> None:
        self.llm    = LLMRouter()
        self.intent = IntentClassifier()
        self.skills = _build_skill_registry()
        self.hooks  = _build_hook_registry()

        from app.agents.registry import AgentRegistry
        self.agents = AgentRegistry()

        from app.memory.memory_manager import MemoryManager
        self.memory = MemoryManager(
            redis_host=settings.redis_host,
            redis_port=settings.redis_port,
            redis_password=settings.redis_password,
            postgres_dsn=settings.postgres_dsn,
            qdrant_host=settings.qdrant_host,
            qdrant_port=settings.qdrant_port,
            qdrant_collection=settings.qdrant_collection,
            flush_interval_turns=settings.memory_flush_interval_turns,
        )

    # ── Public entry point ────────────────────────────────────────────────────

    async def process(self, message: str, session_id: str) -> DispatchResult:
        from app.hooks.base import HookEvent, HookContext

        # 0. Per-session rate limiting (fast Redis check, no LLM cost)
        try:
            rate_limiter.check(session_id)
        except RateLimitExceeded as exc:
            try:
                from app.observability.prometheus_metrics import RATE_LIMITED_TOTAL
                window = "minute" if "minute" in str(exc) else "hour"
                RATE_LIMITED_TOTAL.labels(window=window).inc()
            except Exception:
                pass
            return DispatchResult(
                reply=f"⏱️ {exc}",
                intent="rate_limited",
                session_id=session_id,
            )

        # 1. PRE_PROCESS hooks (security, logging)
        ctx = HookContext(
            session_id=session_id,
            message=message,
            event=HookEvent.PRE_PROCESS,
        )
        ctx = await self.hooks.fire(HookEvent.PRE_PROCESS, ctx)
        if ctx.metadata.get("blocked"):
            reply = ctx.metadata.get(
                "blocked_reply",
                "I can't process that request.",
            )
            return DispatchResult(reply=reply, intent="blocked", session_id=session_id)

        # 2. Get multi-tier memory context
        mem_ctx = await self.memory.get_full_context(session_id, message)
        history = mem_ctx.hot_history

        # 3. Pending action confirmation check
        pending = self.memory.redis.get_pending_action(session_id)
        if pending:
            lower = message.lower().strip()
            words = set(lower.split())
            if words & _CONFIRM_WORDS:
                reply = await self._execute_pending(pending, session_id)
                self.memory.redis.clear_pending_action(session_id)
                await self.memory.persist_turn(session_id, message, reply, intent=pending["intent"])
                return DispatchResult(reply=reply, intent=pending["intent"], session_id=session_id)
            elif words & _CANCEL_WORDS:
                self.memory.redis.clear_pending_action(session_id)
                reply = "Got it — cancelled."
                await self.memory.persist_turn(session_id, message, reply, intent="cancel")
                return DispatchResult(reply=reply, intent="cancel", session_id=session_id)

        # 4. Classify intent (with registry-driven skill descriptions)
        available_skills = self.skills.list_all_descriptions()
        classified = self.intent.classify(message, available_skills=available_skills)
        intent     = classified.get("intent", "chat")
        params     = classified.get("params", {})

        # 5. Select agent
        agent = self.agents.select(intent, message)

        # 6. Execute skill
        skill   = self.skills.get(intent)
        sk_t0   = time.monotonic()
        try:
            result = await skill.execute(params, message)
        except Exception as exc:
            _capture_error(exc, context={"intent": intent, "skill": skill.name, "session_id": session_id})
            logger.error("Skill {} failed for intent {}: {}", skill.name, intent, exc)
            result_context = f"[Skill error — {skill.name} failed: {exc}. Inform the user gracefully.]"
            from app.skills.base import SkillResult
            result = SkillResult(context_data=result_context, skill_name=skill.name)

        sk_latency = round((time.monotonic() - sk_t0) * 1000, 1)
        await event_bus.publish({
            "event":        "skill_dispatched",
            "session_id":   session_id,
            "intent":       intent,
            "skill":        skill.name,
            "has_context":  bool(result.context_data),
            "needs_confirm": bool(result.pending_action),
            "latency_ms":   sk_latency,
        })
        logger.debug(
            "SKILL | {} | intent={} | ctx={} | {}ms",
            skill.name, intent, bool(result.context_data), sk_latency,
        )

        # 7. Build augmented prompt
        augmented = self._build_augmented(
            message, intent, result.context_data,
            mem_ctx.warm_summary, mem_ctx.cold_matches,
        )

        # 8. Call LLM
        try:
            reply = await asyncio.to_thread(self.llm.route, augmented, history, agent)
        except BudgetExceeded as exc:
            try:
                from app.observability.prometheus_metrics import BUDGET_EXCEEDED_TOTAL
                BUDGET_EXCEEDED_TOTAL.inc()
            except Exception:
                pass
            logger.warning("LLM call blocked — budget exceeded: {}", exc)
            reply = (
                "⚠️ I'm temporarily unavailable — the daily API budget has been reached. "
                "I'll be back at midnight UTC. You can check `GET /api/v1/costs` for the breakdown."
            )
            return DispatchResult(
                reply=reply,
                intent=intent,
                session_id=session_id,
                agent=agent.name if agent else "default",
            )
        except Exception as exc:
            _capture_error(exc, context={"intent": intent, "agent": agent.name if agent else "default", "session_id": session_id})
            logger.error("LLM routing failed for session {}: {}", session_id, exc)
            raise

        # 9. Append confirmation instructions if needed
        if result.pending_action:
            reply = f"{reply}\n\n_Reply **confirm** to proceed or **cancel** to abort._"
            self.memory.redis.set_pending_action(session_id, result.pending_action)

        # 10. Persist turn
        await self.memory.persist_turn(session_id, message, reply, intent=intent)

        # 11. POST_PROCESS hooks
        post_ctx = HookContext(
            session_id=session_id,
            message=message,
            reply=reply,
            intent=intent,
            agent_name=agent.name if agent else "default",
            event=HookEvent.POST_PROCESS,
            metadata={"source": ctx.metadata.get("source", "unknown")},
        )
        await self.hooks.fire(HookEvent.POST_PROCESS, post_ctx)

        return DispatchResult(
            reply=reply,
            intent=intent,
            session_id=session_id,
            agent=agent.name if agent else "default",
        )

    # ── Prompt assembly ───────────────────────────────────────────────────────

    def _build_augmented(
        self,
        message: str,
        intent: str,
        context_data: str,
        warm_summary: str,
        cold_matches: list[dict],
    ) -> str:
        parts: list[str] = []

        if warm_summary:
            parts.append(f"[Session summary from prior conversations]:\n{warm_summary}")

        if cold_matches:
            import json
            parts.append(f"[Relevant past context]:\n{json.dumps(cold_matches, indent=2)}")

        if context_data:
            parts.append(
                f"[Live data from {intent}]:\n{context_data}\n\n"
                "Respond to the user naturally using the data above. "
                "Format clearly — use bullet points or short paragraphs as appropriate."
            )

        parts.append(f"User message: {message}")
        return "\n\n".join(parts)

    # ── Pending action execution ──────────────────────────────────────────────

    async def _execute_pending(self, pending: dict, session_id: str) -> str:
        action = pending.get("action")
        params = pending.get("params", {})

        try:
            if action == "send_email":
                from app.integrations.gmail import GmailClient
                result = await GmailClient().send_email(
                    to      = params.get("to", ""),
                    subject = params.get("subject", ""),
                    body    = params.get("drafted_body", params.get("body_hint", "")),
                )
                return f"Email sent. Message ID: `{result.get('id', 'unknown')}`"

            if action == "create_calendar_event":
                from app.integrations.google_calendar import CalendarClient
                result = await CalendarClient().create_event(params)
                title = result.get("title", params.get("title", "Event"))
                start = result.get("start", "")
                link  = result.get("link", "")
                return (
                    f"Done! **{title}** has been added to your calendar.\n"
                    f"Start: `{start}`\n"
                    + (f"[Open in Google Calendar]({link})" if link else "")
                )

            return f"[Unknown pending action: {action}]"
        except Exception as exc:
            logger.error("Failed to execute pending action %s: %s", action, exc)
            return f"Failed to execute: {exc}"
