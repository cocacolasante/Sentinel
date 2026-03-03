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
import uuid
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
    from app.skills.repo_skill      import RepoReadSkill, RepoWriteSkill, RepoCommitSkill
    from app.skills.gmail_skill     import GmailReplySkill
    from app.skills.contacts_skill  import ContactsReadSkill, ContactsWriteSkill
    from app.skills.ionos_skill     import IONOSCloudSkill, IONOSDNSSkill
    from app.skills.cicd_skill      import CICDReadSkill, CICDTriggerSkill
    from app.skills.n8n_skill       import N8nManageSkill
    from app.skills.whatsapp_skill  import WhatsAppReadSkill, WhatsAppSendSkill
    from app.skills.skill_discovery    import SkillDiscoverySkill
    from app.skills.sentry_skill       import SentryReadSkill, SentryManageSkill
    from app.skills.server_shell_skill import ServerShellSkill

    reg = SkillRegistry()
    reg.register(ChatSkill())
    # Gmail
    reg.register(GmailReadSkill())
    reg.register(GmailSendSkill())
    reg.register(GmailReplySkill())
    # Calendar
    reg.register(CalendarReadSkill())
    reg.register(CalendarWriteSkill())
    # GitHub
    reg.register(GitHubReadSkill())
    reg.register(GitHubWriteSkill())
    # Smart home
    reg.register(SmartHomeSkill())
    # n8n
    reg.register(N8nSkill())
    reg.register(N8nManageSkill())
    # CI/CD
    reg.register(CICDReadSkill())
    reg.register(CICDTriggerSkill())
    # Contacts / address book
    reg.register(ContactsReadSkill())
    reg.register(ContactsWriteSkill())
    # WhatsApp
    reg.register(WhatsAppReadSkill())
    reg.register(WhatsAppSendSkill())
    # IONOS Cloud + DNS
    reg.register(IONOSCloudSkill())
    reg.register(IONOSDNSSkill())
    # Content / writing
    reg.register(ResearchSkill())
    reg.register(CodeSkill())
    reg.register(ContentDraftSkill())
    reg.register(SocialCaptionSkill())
    reg.register(AdCopySkill())
    reg.register(ContentRepurposeSkill())
    reg.register(ContentCalendarSkill())
    # Repo self-modification
    reg.register(RepoReadSkill())
    reg.register(RepoWriteSkill())
    reg.register(RepoCommitSkill())
    # Skill discovery
    reg.register(SkillDiscoverySkill())
    # Sentry error tracking
    reg.register(SentryReadSkill())
    reg.register(SentryManageSkill())
    # Server shell — filesystem navigation, builds, project scaffolding
    reg.register(ServerShellSkill())
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
                # _execute_pending updates write task status internally on success/fail
                await self.memory.persist_turn(session_id, message, reply, intent=pending["intent"])
                return DispatchResult(reply=reply, intent=pending["intent"], session_id=session_id)
            elif words & _CANCEL_WORDS:
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "cancelled")
                self.memory.redis.clear_pending_action(session_id)
                reply = "Got it — cancelled."
                await self.memory.persist_turn(session_id, message, reply, intent="cancel")
                return DispatchResult(reply=reply, intent="cancel", session_id=session_id)

        # 4. Classify intent (with registry-driven skill descriptions)
        available_skills = self.skills.list_all_descriptions()
        classified = self.intent.classify(message, available_skills=available_skills)
        intent     = classified.get("intent", "chat")
        params     = classified.get("params", {})
        confidence = classified.get("confidence", 1.0)

        # 4a. Contact resolution — if params contain a name (not an email/phone),
        #     look it up in the address book and substitute the resolved value.
        params = await self._resolve_contact_params(intent, params)

        # 4b. Skill discovery — if confidence is very low and message looks action-oriented
        from app.skills.skill_discovery import SkillGapHandler
        if SkillGapHandler.should_trigger(intent, confidence, message):
            intent = "skill_discover"
            params = {}

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

        # 9. Approval-level gate — decide whether to confirm, auto-execute, or skip
        if result.pending_action:
            approval_level = self.memory.redis.get_approval_level()
            needs_confirm  = self._needs_confirmation(skill, approval_level)
            task_id        = str(uuid.uuid4())

            # Always log the write task for the dashboard audit trail
            self._log_write_task(task_id, session_id, result.pending_action, skill)

            if needs_confirm:
                # Store for session-based confirmation flow
                result.pending_action["_task_id"] = task_id
                reply = f"{reply}\n\n_Reply **confirm** to proceed or **cancel** to abort._"
                self.memory.redis.set_pending_action(session_id, result.pending_action)
            else:
                # Auto-execute — approval level says this write doesn't need confirmation
                logger.info(
                    "Auto-executing write action | action={} | level={} | category={}",
                    result.pending_action.get("action"), approval_level,
                    getattr(skill, "approval_category", "unknown"),
                )
                self._update_write_task_status(task_id, "executing")
                try:
                    exec_reply = await self._execute_pending(result.pending_action, session_id)
                    self._update_write_task_status(task_id, "completed")
                    reply = exec_reply
                except Exception as exc:
                    self._update_write_task_status(task_id, "failed", str(exc))
                    reply = (
                        f"Write action failed automatically: `{exc}`\n"
                        "Nothing was sent or saved."
                    )

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

    # ── Approval helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _needs_confirmation(skill, approval_level: int) -> bool:
        """Return True if this skill's write action requires user confirmation at the given level."""
        from app.skills.base import ApprovalCategory
        cat = getattr(skill, "approval_category", ApprovalCategory.NONE)
        if cat == ApprovalCategory.NONE:
            return False
        if cat == ApprovalCategory.BREAKING:
            return True                   # always confirm
        if cat == ApprovalCategory.CRITICAL:
            return approval_level <= 2    # confirm at levels 1 & 2
        # STANDARD
        return approval_level <= 1        # confirm only at level 1

    @staticmethod
    def _log_write_task(task_id: str, session_id: str, pending: dict, skill) -> None:
        """Insert a pending_write_tasks row for the dashboard audit trail."""
        try:
            from app.db import postgres
            from app.skills.base import ApprovalCategory
            cat = getattr(skill, "approval_category", ApprovalCategory.STANDARD).value
            import json
            postgres.execute(
                """
                INSERT INTO pending_write_tasks
                       (task_id, session_id, action, title, params, category, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'awaiting_approval')
                ON CONFLICT DO NOTHING
                """,
                (
                    task_id,
                    session_id,
                    pending.get("action", "unknown"),
                    pending.get("original", "")[:200],
                    json.dumps(pending.get("params", {})),
                    cat,
                ),
            )
        except Exception as exc:
            logger.warning("Could not log write task: {}", exc)

    @staticmethod
    def _update_write_task_status(task_id: str, status: str, error: str | None = None) -> None:
        try:
            from app.db import postgres
            postgres.execute(
                "UPDATE pending_write_tasks SET status=%s, error=%s, updated_at=NOW() WHERE task_id=%s",
                (status, error, task_id),
            )
        except Exception as exc:
            logger.warning("Could not update write task status: {}", exc)

    # ── Contact resolution ────────────────────────────────────────────────────

    @staticmethod
    async def _resolve_contact_params(intent: str, params: dict) -> dict:
        """
        For intents that send to a person, resolve a contact name to email/phone
        if the param doesn't already look like a real address.
        """
        try:
            from app.integrations.contacts import ContactsClient
            client = ContactsClient()

            # Email-based intents: resolve 'to' field
            if intent in ("gmail_send", "gmail_reply", "calendar_write"):
                to_val = params.get("to", "")
                if to_val and "@" not in to_val:
                    email = await client.resolve_to_email(to_val)
                    if email:
                        logger.info("Contact resolved | name={} | email={}", to_val, email)
                        params = {**params, "to": email}

            # Attendees list
            if intent == "calendar_write" and params.get("attendees"):
                resolved = []
                for a in params["attendees"]:
                    if "@" not in a:
                        email = await client.resolve_to_email(a)
                        resolved.append(email if email else a)
                    else:
                        resolved.append(a)
                params = {**params, "attendees": resolved}

            # WhatsApp: resolve 'to' to phone
            if intent == "whatsapp_send":
                to_val = params.get("to", "")
                if to_val and not to_val.lstrip("+").isdigit() and not to_val.startswith("whatsapp:"):
                    phone = await client.resolve_to_phone(to_val)
                    if phone:
                        logger.info("Contact resolved | name={} | phone={}", to_val, phone)
                        params = {**params, "to": phone}

        except Exception as exc:
            logger.debug("Contact resolution skipped: {}", exc)

        return params

    # ── Pending action execution ──────────────────────────────────────────────

    async def _execute_pending(self, pending: dict, session_id: str) -> str:
        action   = pending.get("action")
        params   = pending.get("params", {})
        original = pending.get("original", "")

        try:
            if action == "send_email":
                from app.integrations.gmail import get_gmail_client
                to      = params.get("to", "")
                subject = params.get("subject", "")
                account = params.get("account")
                client  = get_gmail_client(account_name=account)

                # Generate a properly written email body (not just the raw hint)
                draft_prompt = (
                    f"Write the body of a professional, concise email.\n"
                    f"To: {to}\n"
                    f"Subject: {subject}\n"
                    f"Intent: {params.get('body_hint', original)}\n\n"
                    "Output ONLY the email body — no subject line, no metadata. "
                    "Use an appropriate greeting and sign-off."
                )
                body = await asyncio.to_thread(self.llm.route, draft_prompt, None, None)

                result = await client.send_email(to=to, subject=subject, body=body)
                msg_id = result.get("id", "unknown")
                logger.info("Email sent | account={} | to={} | subject={} | msg_id={}", client.account_name, to, subject, msg_id)
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return f"Email sent from **{client.account_name}** to **{to}**.\nSubject: _{subject}_\nMessage ID: `{msg_id}`"

            if action == "create_calendar_event":
                from app.integrations.google_calendar import get_calendar_client
                from app.integrations.gmail import get_gmail_client
                account  = params.get("account")
                cal      = get_calendar_client(account_name=account)
                result   = await cal.create_event(params)
                title    = result.get("title", params.get("title", "Event"))
                start    = result.get("start", "")
                link     = result.get("link", "")
                attendees = result.get("attendees", [])
                logger.info("Calendar event created | account={} | title={} | start={} | attendees={}", cal.account_name, title, start, attendees)

                # Send a personal Gmail invite to each attendee (use same account)
                gmail_client  = get_gmail_client(account_name=account)
                gmail_results = []
                for email in attendees:
                    invite_body = await asyncio.to_thread(
                        self.llm.route,
                        (
                            f"Write a short, friendly email inviting someone to: {title}\n"
                            f"When: {start}\n"
                            f"Keep it warm and concise — 2-3 sentences max. "
                            "Output ONLY the email body."
                        ),
                        None, None,
                    )
                    await gmail_client.send_email(
                        to=email,
                        subject=f"Invitation: {title}",
                        body=invite_body,
                    )
                    gmail_results.append(email)
                    logger.info("Invite email sent | to={} | event={}", email, title)

                reply = (
                    f"Done! **{title}** has been added to the **{cal.account_name}** calendar.\n"
                    f"Start: `{start}`\n"
                    + (f"[Open in Google Calendar]({link})\n" if link else "")
                )
                if gmail_results:
                    reply += f"\nInvite email sent from your Gmail to: {', '.join(gmail_results)}"
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return reply

            if action in ("write_file", "patch_file"):
                from app.integrations.repo import RepoClient
                client = RepoClient()
                await client.ensure_repo()
                if action == "patch_file":
                    result_msg = await client.patch_file(
                        params.get("path", ""),
                        params.get("old", ""),
                        params.get("new", ""),
                    )
                else:
                    result_msg = await client.write_file(
                        params.get("path", ""),
                        params.get("content", ""),
                    )
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                logger.info("Repo file written | path={}", params.get("path"))
                return f"Done. {result_msg}\nUse `commit these changes` to save to GitHub."

            if action in ("commit", "commit_push", "push"):
                from app.integrations.repo import RepoClient
                client = RepoClient()
                await client.ensure_repo()
                message = params.get("message", "Brain: automated update")
                output_parts = []
                if action in ("commit", "commit_push"):
                    out = await client.commit(message)
                    output_parts.append(out)
                if action in ("push", "commit_push"):
                    out = await client.push()
                    output_parts.append(f"Pushed: {out}")
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                logger.info("Repo commit/push | action={} | message={}", action, message)
                return "\n".join(output_parts) or "Done."

            if action == "reply_email":
                from app.integrations.gmail import get_gmail_client
                account = params.get("account")
                client  = get_gmail_client(account_name=account)
                msg_id  = params.get("msg_id", "")
                draft_prompt = (
                    f"Write a reply email.\n"
                    f"Context: {params.get('body_hint', original)}\n\n"
                    "Output ONLY the reply body — no subject line, no metadata. "
                    "Use an appropriate tone."
                )
                body   = await asyncio.to_thread(self.llm.route, draft_prompt, None, None)
                result = await client.reply_email(msg_id=msg_id, body=body)
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return (
                    f"Reply sent in-thread from **{client.account_name}** to **{result.get('to', '?')}**.\n"
                    f"Thread ID: `{result.get('thread_id', '?')}`"
                )

            if action == "shell_exec":
                from app.skills.server_shell_skill import _run_command
                command = params.get("command", "").strip()
                cwd     = params.get("cwd", "/root").rstrip("/") or "/root"
                if not command:
                    return "[shell_exec: no command provided]"
                output, code = await _run_command(command, cwd)
                status = "✅ exit 0" if code == 0 else f"⚠️ exit {code}"
                logger.info("Shell exec confirmed | cwd={} | cmd={} | code={}", cwd, command, code)
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return (
                    f"Command executed.\n"
                    f"```bash\n$ {command}\n```\n"
                    f"Working directory: `{cwd}`\n"
                    f"Status: {status}\n\n"
                    f"Output:\n```\n{output or '(no output)'}\n```"
                )

            if action in ("add_contact", "update_contact", "delete_contact") or (
                action in ("add", "update", "delete") and pending.get("intent") == "contacts_write"
            ):
                from app.integrations.contacts import ContactsClient
                client = ContactsClient()
                real_action = params.get("action", action)

                if real_action == "add":
                    name = params.get("name", "")
                    extra = {k: params.get(k, "") for k in
                             ("email", "phone", "whatsapp", "company", "github", "slack_id", "tags", "notes")}
                    result = await client.add(name, **extra)
                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")
                    return f"Contact **{name}** added (ID: {result.get('id')})."

                if real_action == "update":
                    contact_id = int(params.get("id", 0))
                    fields     = {k: v for k, v in params.items()
                                  if k not in ("action", "id") and v}
                    result = await client.update(contact_id, fields)
                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")
                    return f"Contact {contact_id} updated: {result.get('name', '?')}"

                if real_action == "delete":
                    contact_id = int(params.get("id", 0))
                    await client.delete(contact_id)
                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")
                    return f"Contact {contact_id} deleted."

            if action == "send_whatsapp":
                from app.integrations.whatsapp import WhatsAppClient
                to   = params.get("to", "")
                body = params.get("body", "")
                if not body:
                    draft_prompt = (
                        f"Write a short WhatsApp message.\n"
                        f"Intent: {original}\n\n"
                        "Output ONLY the message body — keep it concise and conversational."
                    )
                    body = await asyncio.to_thread(self.llm.route, draft_prompt, None, None)
                result = await WhatsAppClient().send(to=to, body=body)
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return f"WhatsApp message sent to **{to}** (SID: `{result.get('sid', '?')}`)."

            if action == "trigger_workflow":
                from app.integrations.github import GitHubClient
                from app.config import get_settings as _gs
                _settings = _gs()
                repo        = params.get("repo", _settings.github_default_repo)
                workflow_id = params.get("workflow_id", params.get("workflow_name", ""))
                ref         = params.get("ref", "main")
                inputs      = params.get("inputs", {})
                result = await GitHubClient().trigger_workflow(repo, workflow_id, ref, inputs)
                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return (
                    f"Workflow **{workflow_id}** triggered on `{repo}` (branch: `{ref}`).\n"
                    "Check GitHub Actions for the run status."
                )

            if action in ("ionos_create_datacenter", "create_datacenter",
                          "ionos_start_server", "start_server",
                          "ionos_stop_server", "stop_server",
                          "ionos_reboot_server", "reboot_server",
                          "ionos_create_server", "create_server",
                          "ionos_delete_server", "delete_server",
                          "ionos_delete_datacenter", "delete_datacenter",
                          "ionos_ssh_exec", "ssh_exec",
                          "ionos_deploy_docker", "deploy_docker",
                          "ionos_configure_server", "configure_server",
                          "ionos_reserve_ip", "reserve_ip") or (
                pending.get("intent") == "ionos_cloud"
            ):
                from app.integrations.ionos import IONOSClient
                import json as _json
                client    = IONOSClient()
                dc_id     = params.get("datacenter_id", "")
                server_id = params.get("server_id", "")
                real_act  = params.get("action", action)

                if real_act == "create_datacenter":
                    res = await client.create_datacenter(
                        params.get("name", "brain-dc"),
                        params.get("location", "us/las"),
                        params.get("description", ""),
                    )
                elif real_act == "create_server":
                    res = await client.create_server(
                        dc_id, params.get("name", "brain-server"),
                        int(params.get("cores", 1)), int(params.get("ram_mb", 1024)),
                    )
                elif real_act == "start_server":
                    res = await client.start_server(dc_id, server_id)
                elif real_act == "stop_server":
                    res = await client.stop_server(dc_id, server_id)
                elif real_act == "reboot_server":
                    res = await client.reboot_server(dc_id, server_id)
                elif real_act == "delete_server":
                    res = await client.delete_server(dc_id, server_id)
                elif real_act == "delete_datacenter":
                    res = await client.delete_datacenter(dc_id)
                elif real_act == "ssh_exec":
                    res = await client.ssh_exec(
                        params.get("host", ""), params.get("command", ""),
                        params.get("username", "root"), int(params.get("port", 22)),
                    )
                elif real_act == "deploy_docker":
                    res = await client.deploy_docker_app(
                        params.get("host", ""), params.get("image", ""),
                        params.get("container_name", "app"), params.get("port_map", "80:80"),
                        params.get("env_vars"), params.get("username", "root"),
                    )
                elif real_act == "configure_server":
                    res = await client.configure_server(
                        params.get("host", ""), params.get("commands", []),
                        params.get("username", "root"),
                    )
                elif real_act == "reserve_ip":
                    res = await client.reserve_ip(
                        params.get("location", "us/las"), int(params.get("size", 1)),
                    )
                else:
                    res = {"error": f"Unknown IONOS action: {real_act}"}

                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return f"IONOS `{real_act}` completed:\n```json\n{_json.dumps(res, indent=2)}\n```"

            if pending.get("intent") == "ionos_dns":
                from app.integrations.ionos_dns import IONOSDNSClient
                import json as _json
                client   = IONOSDNSClient()
                zone_id  = params.get("zone_id", "")
                real_act = params.get("action", action)

                if real_act == "create_record":
                    res = await client.create_record(
                        zone_id, params.get("name", "@"), params.get("type", "A"),
                        params.get("content", ""), int(params.get("ttl", 3600)),
                    )
                elif real_act == "update_record":
                    res = await client.update_record(zone_id, params.get("record_id", ""),
                                                     {k: params[k] for k in ("content", "ttl", "enabled") if k in params})
                elif real_act == "delete_record":
                    res = await client.delete_record(zone_id, params.get("record_id", ""))
                elif real_act == "upsert_record":
                    res = await client.upsert_record(
                        params.get("zone_name", ""), params.get("name", "@"),
                        params.get("type", "A"), params.get("content", ""),
                        int(params.get("ttl", 3600)),
                    )
                elif real_act == "create_zone":
                    res = await client.create_zone(params.get("zone_name", ""))
                else:
                    res = {"error": f"Unknown DNS action: {real_act}"}

                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return f"DNS `{real_act}` completed:\n```json\n{_json.dumps(res, indent=2)}\n```"

            if pending.get("intent") == "n8n_manage":
                from app.integrations.n8n_bridge import N8nBridge
                import json as _json
                bridge   = N8nBridge()
                real_act = params.get("action", action)
                wf_id    = params.get("workflow_id", "")

                if real_act == "create":
                    res = await bridge.create_workflow(
                        params.get("name", "New Workflow"),
                        params.get("nodes", []),
                        params.get("connections"),
                    )
                elif real_act == "activate":
                    res = await bridge.activate_workflow(wf_id)
                elif real_act == "deactivate":
                    res = await bridge.deactivate_workflow(wf_id)
                elif real_act == "delete":
                    res = await bridge.delete_workflow(wf_id)
                else:
                    res = {"error": f"Unknown n8n manage action: {real_act}"}

                task_id = pending.get("_task_id")
                if task_id:
                    self._update_write_task_status(task_id, "completed")
                return f"n8n `{real_act}` completed:\n```json\n{_json.dumps(res, indent=2)}\n```"

            # ── Sentry issue management ──────────────────────────────────────
            if action in ("sentry_resolve", "sentry_ignore", "sentry_assign",
                          "sentry_comment", "sentry_investigate"):
                from app.integrations.sentry_client import SentryClient
                client   = SentryClient()
                issue_id = params.get("issue_id", "")

                if action == "sentry_investigate":
                    try:
                        issue = await client.get_issue(issue_id) if client.is_configured() else params
                    except Exception:
                        issue = params  # fall back to webhook params if API not configured

                    level     = issue.get("level", "error")
                    title     = issue.get("title", params.get("title", ""))
                    project   = issue.get("project", params.get("project", ""))
                    count     = issue.get("count", params.get("count", 0))
                    permalink = issue.get("permalink", params.get("permalink", ""))
                    platform  = issue.get("platform", params.get("platform", ""))
                    culprit   = issue.get("culprit", "")

                    analysis_prompt = (
                        f"A {level.upper()} error has been reported in the {project} project.\n\n"
                        f"**Title:** {title}\n"
                        f"**Platform:** {platform}\n"
                        f"**Occurrences:** {count}\n"
                        f"**Culprit:** {culprit}\n"
                        f"**Sentry link:** {permalink}\n\n"
                        "Analyze this error and provide:\n"
                        "1. Likely root cause\n"
                        "2. Immediate mitigation steps\n"
                        "3. Recommended fix\n"
                        "4. Suggested next action (resolve, create GitHub issue, assign, etc.)"
                    )
                    analysis = await self.llm.generate(analysis_prompt, task_type="reasoning")

                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")

                    badge = {"fatal": "🔴", "critical": "🔴", "error": "🟠", "warning": "🟡"}.get(level, "🟠")
                    return f"{badge} **Sentry {level.upper()} Analysis** — `{issue_id}`\n" + analysis

                elif action == "sentry_resolve":
                    await client.resolve_issue(issue_id)
                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")
                    return f"✅ Sentry issue `{issue_id}` marked as **resolved**."

                elif action == "sentry_ignore":
                    await client.ignore_issue(issue_id)
                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")
                    return f"🔇 Sentry issue `{issue_id}` marked as **ignored**."

                elif action == "sentry_assign":
                    assignee = params.get("assignee", "")
                    await client.assign_issue(issue_id, assignee)
                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")
                    return f"👤 Sentry issue `{issue_id}` assigned to **{assignee}**."

                elif action == "sentry_comment":
                    text = params.get("text", "")
                    await client.add_note(issue_id, text)
                    task_id = pending.get("_task_id")
                    if task_id:
                        self._update_write_task_status(task_id, "completed")
                    return f"💬 Note added to Sentry issue `{issue_id}`."

            return f"[Unknown pending action: {action}]"

        except Exception as exc:
            logger.error("Pending action failed | action={} | error={}", action, exc)
            _capture_error(exc, context={"action": action, "session_id": session_id})
            task_id = pending.get("_task_id")
            if task_id:
                self._update_write_task_status(task_id, "failed", str(exc))
            # Return a clear failure — never silently claim success
            return (
                f"Something went wrong executing **{action}** — nothing was sent or saved.\n"
                f"Error: `{type(exc).__name__}: {exc}`\n\n"
                "Please try again, or check your Google credentials in `.env`."
            )
