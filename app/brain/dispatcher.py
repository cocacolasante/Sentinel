"""
Brain Dispatcher — the central orchestrator for Phase 2.

Flow for each incoming message:
  1. Check Redis for a pending write-action awaiting confirmation
  2. If confirming/cancelling → execute or abort pending action
  3. Otherwise classify intent via IntentClassifier (Haiku)
  4. If integration intent → call integration, build context string
  5. Augment the message with context → call LLM router
  6. Store (user, assistant) exchange in Redis hot memory
  7. Return reply to caller (REST or Slack)

Write actions that need confirmation (email send) store a PendingAction in
Redis with a 5-minute TTL. The user says "send it", "confirm", "yes", etc.
Reversible or low-risk writes (calendar, smart home, GitHub issues) execute
immediately.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

from app.brain.intent      import IntentClassifier
from app.brain.llm_router  import LLMRouter
from app.memory.redis_client import RedisMemory
from app.config            import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

# Lazy-import integrations so missing deps don't crash the whole app
def _load_integrations():
    from app.integrations.gmail            import GmailClient
    from app.integrations.google_calendar  import CalendarClient
    from app.integrations.github           import GitHubClient
    from app.integrations.n8n_bridge       import N8nBridge
    from app.integrations.home_assistant   import HomeAssistantClient
    return GmailClient(), CalendarClient(), GitHubClient(), N8nBridge(), HomeAssistantClient()


# ── Confirmation trigger words ────────────────────────────────────────────────
_CONFIRM_WORDS = {"confirm", "send", "yes", "do it", "proceed", "go ahead", "send it"}
_CANCEL_WORDS  = {"cancel", "no", "stop", "abort", "nevermind", "never mind", "don't"}

PENDING_TTL = 300  # 5 minutes


@dataclass
class DispatchResult:
    reply:   str
    intent:  str
    session_id: str


class Dispatcher:
    def __init__(self) -> None:
        self.llm    = LLMRouter()
        self.intent = IntentClassifier()
        self.memory = RedisMemory()
        self._gmail, self._calendar, self._github, self._n8n, self._ha = _load_integrations()

    # ── Public entry point ────────────────────────────────────────────────────

    async def process(self, message: str, session_id: str) -> DispatchResult:
        history = self.memory.get_history(session_id)

        # 1. Pending action confirmation check
        pending = self.memory.get_pending_action(session_id)
        if pending:
            lower = message.lower().strip()
            words = set(lower.split())
            if words & _CONFIRM_WORDS:
                reply = await self._execute_pending(pending, session_id)
                self.memory.clear_pending_action(session_id)
                self.memory.append_turn(session_id, message, reply)
                return DispatchResult(reply=reply, intent=pending["intent"], session_id=session_id)
            elif words & _CANCEL_WORDS:
                self.memory.clear_pending_action(session_id)
                reply = "Got it — cancelled."
                self.memory.append_turn(session_id, message, reply)
                return DispatchResult(reply=reply, intent="cancel", session_id=session_id)

        # 2. Classify intent
        classified = self.intent.classify(message)
        intent     = classified.get("intent", "chat")
        params     = classified.get("params", {})

        # 3. Dispatch to integration or pure LLM
        context_data, pending_action = await self._dispatch(intent, params, message)

        # 4. Build augmented prompt
        if context_data:
            augmented = (
                f"[Live data from {intent}]:\n{context_data}\n\n"
                f"User message: {message}\n\n"
                "Respond to the user naturally using the data above. "
                "Format clearly — use bullet points or short paragraphs as appropriate."
            )
        else:
            augmented = message

        # 5. Call LLM
        reply = await asyncio.to_thread(self.llm.route, augmented, history)

        # 6. If write action needs confirmation, append instructions and store pending
        if pending_action:
            reply = f"{reply}\n\n_Reply **confirm** to proceed or **cancel** to abort._"
            self.memory.set_pending_action(session_id, pending_action)

        # 7. Persist to Redis
        self.memory.append_turn(session_id, message, reply)

        return DispatchResult(reply=reply, intent=intent, session_id=session_id)

    # ── Intent dispatch table ─────────────────────────────────────────────────

    async def _dispatch(
        self,
        intent: str,
        params: dict,
        original_message: str,
    ) -> tuple[str, dict | None]:
        """
        Returns (context_data, pending_action).
        context_data:   string injected into the LLM prompt (empty for pure chat)
        pending_action: dict stored in Redis pending confirmation (None if not needed)
        """
        handlers = {
            "gmail_read":     self._gmail_read,
            "gmail_send":     self._gmail_send,
            "calendar_read":  self._calendar_read,
            "calendar_write": self._calendar_write,
            "github_read":    self._github_read,
            "github_write":   self._github_write,
            "smart_home":     self._smart_home,
            "n8n_execute":    self._n8n_execute,
        }
        handler = handlers.get(intent)
        if handler:
            try:
                return await handler(params, original_message)
            except Exception as exc:
                logger.error("Integration handler %s failed: %s", intent, exc)
                return (
                    f"[Error fetching {intent} data: {exc}. "
                    "Inform the user there was an issue and suggest they check the integration config.]",
                    None,
                )
        return "", None  # chat intent

    # ── Integration handlers ──────────────────────────────────────────────────

    async def _gmail_read(self, params: dict, _msg: str) -> tuple[str, None]:
        if not self._gmail.is_configured():
            return "[Gmail not configured — GOOGLE_REFRESH_TOKEN missing in .env]", None
        query       = params.get("query", "is:unread")
        max_results = int(params.get("max_results", 10))
        emails = await self._gmail.list_emails(query=query, max_results=max_results)
        return json.dumps(emails, indent=2), None

    async def _gmail_send(self, params: dict, original_message: str) -> tuple[str, dict]:
        if not self._gmail.is_configured():
            return "[Gmail not configured]", None
        # Store params + original message in pending — LLM will draft the body
        pending = {
            "intent":   "gmail_send",
            "action":   "send_email",
            "params":   params,
            "original": original_message,
        }
        context = (
            f"Draft an email based on the user's request. "
            f"Recipient: {params.get('to', 'unknown')}. "
            f"Subject hint: {params.get('subject', '')}. "
            f"Content hint: {params.get('body_hint', original_message)}. "
            "Show the full draft (To, Subject, Body) formatted clearly. "
            "Explain that the user must confirm before it is sent."
        )
        return context, pending

    async def _calendar_read(self, params: dict, _msg: str) -> tuple[str, None]:
        if not self._calendar.is_configured():
            return "[Google Calendar not configured — GOOGLE_REFRESH_TOKEN missing]", None
        period = params.get("period", "this week")
        events = await self._calendar.list_events(period=period)
        return json.dumps(events, indent=2), None

    async def _calendar_write(self, params: dict, _msg: str) -> tuple[str, None]:
        if not self._calendar.is_configured():
            return "[Google Calendar not configured]", None
        result = await self._calendar.create_event(params)
        return json.dumps(result, indent=2), None

    async def _github_read(self, params: dict, _msg: str) -> tuple[str, None]:
        if not self._github.is_configured():
            return "[GitHub not configured — GITHUB_TOKEN missing]", None
        resource = params.get("resource", "notifications")
        repo     = params.get("repo", "")
        if resource == "issues":
            data = await self._github.list_issues(repo)
        elif resource == "prs":
            data = await self._github.list_prs(repo)
        else:
            data = await self._github.list_notifications()
        return json.dumps(data, indent=2), None

    async def _github_write(self, params: dict, _msg: str) -> tuple[str, None]:
        if not self._github.is_configured():
            return "[GitHub not configured]", None
        action = params.get("action", "create_issue")
        if action == "create_issue":
            result = await self._github.create_issue(
                repo  = params.get("repo", settings.github_default_repo),
                title = params.get("title", "New issue"),
                body  = params.get("body", ""),
            )
            return json.dumps(result, indent=2), None
        return f"[GitHub action '{action}' not yet implemented]", None

    async def _smart_home(self, params: dict, _msg: str) -> tuple[str, None]:
        if not self._ha.is_configured():
            return "[Home Assistant not configured — HOME_ASSISTANT_URL or HOME_ASSISTANT_TOKEN missing]", None
        action = params.get("action", "status")
        entity = params.get("entity", "")
        if action == "status":
            data = await self._ha.get_entity(entity) if entity else await self._ha.get_all_states()
        else:
            # Map action to HA service call
            domain  = entity.split(".")[0] if "." in entity else "homeassistant"
            service = action  # e.g. "turn_on", "turn_off", "toggle"
            value   = params.get("value")
            svc_data = {"entity_id": entity}
            if value is not None:
                svc_data["value"] = value
            data = await self._ha.call_service(domain, service, svc_data)
        return json.dumps(data, indent=2), None

    async def _n8n_execute(self, params: dict, _msg: str) -> tuple[str, None]:
        workflow = params.get("workflow", "")
        payload  = params.get("payload", {})
        result   = await self._n8n.trigger(workflow, payload)
        return json.dumps(result, indent=2), None

    # ── Pending action execution ──────────────────────────────────────────────

    async def _execute_pending(self, pending: dict, session_id: str) -> str:
        action = pending.get("action")
        params = pending.get("params", {})

        try:
            if action == "send_email":
                result = await self._gmail.send_email(
                    to      = params.get("to", ""),
                    subject = params.get("subject", ""),
                    body    = params.get("drafted_body", params.get("body_hint", "")),
                )
                return f"Email sent. Message ID: `{result.get('id', 'unknown')}`"
            return f"[Unknown pending action: {action}]"
        except Exception as exc:
            logger.error("Failed to execute pending action %s: %s", action, exc)
            return f"Failed to execute: {exc}"
