"""
Intent Classifier

Uses Claude Haiku to classify each message into a structured intent with
extracted parameters. This drives the Phase 2 integration routing.

Intents:
  gmail_read       — read / search inbox
  gmail_send       — compose / send / draft email
  calendar_read    — check schedule or availability
  calendar_write   — create / update / delete event
  github_read      — list issues, PRs, notifications, repo info
  github_write     — create issue, comment, close issue
  smart_home       — control or query HA devices
  n8n_execute      — trigger a named n8n workflow
  chat             — general reasoning / writing / coding (no external action)
"""

import json
import logging

import anthropic

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are an intent classification engine for a personal AI assistant. "
    "Classify the user message into exactly one intent and extract relevant "
    "parameters as JSON. Be precise. Never add explanations — return only JSON."
)

_USER_TEMPLATE = """Classify this message into one of these intents:

gmail_read    — read, check, or search emails
gmail_send    — compose, send, or draft an email
calendar_read — check schedule, events, or availability
calendar_write — create, update, reschedule, or cancel a calendar event
github_read   — check issues, PRs, notifications, or repo info
github_write  — create an issue, comment on a PR, close an issue
smart_home    — control or query a smart home device (lights, thermostat, locks, etc.)
n8n_execute   — run a specific n8n workflow by name
chat          — anything else: analysis, writing, code, questions, conversation

Return ONLY valid JSON matching this schema exactly:
{{
  "intent": "<intent_name>",
  "confidence": <0.0-1.0>,
  "params": {{
    // intent-specific extracted fields — empty object if none
  }}
}}

Intent-specific param examples:
  gmail_read:    {{"query": "from:boss", "max_results": 5}}
  gmail_send:    {{"to": "sarah@co.com", "subject": "Re: meeting", "body_hint": "I'll be 10 min late"}}
  calendar_read: {{"period": "today" | "tomorrow" | "this week" | "next week"}}
  calendar_write:{{"title": "Sprint review", "date": "2026-03-05", "time": "14:00", "duration_min": 60, "description": ""}}
  github_read:   {{"repo": "owner/name", "resource": "issues" | "prs" | "notifications"}}
  github_write:  {{"repo": "owner/name", "action": "create_issue", "title": "...", "body": "..."}}
  smart_home:    {{"action": "turn_on" | "turn_off" | "toggle" | "set" | "status", "entity": "light.living_room", "value": null}}
  n8n_execute:   {{"workflow": "daily_brief", "payload": {{}}}}
  chat:          {{}}

Message: {message}"""


# ── Classifier ────────────────────────────────────────────────────────────────

class IntentClassifier:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def classify(self, message: str) -> dict:
        """
        Classify a message and return a dict with keys:
          intent (str), confidence (float), params (dict)
        Falls back to {"intent": "chat", "confidence": 0.5, "params": {}} on any error.
        """
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": _USER_TEMPLATE.format(message=message),
                }],
            )
            raw = response.content[0].text.strip()
            # Strip any accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)
            logger.debug("Intent: %s (%.2f) params=%s", result.get("intent"), result.get("confidence"), result.get("params"))
            return result
        except Exception as exc:
            logger.warning("Intent classification failed (%s) — defaulting to chat", exc)
            return {"intent": "chat", "confidence": 0.5, "params": {}}
