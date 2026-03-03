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
from datetime import date as _date

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

_USER_TEMPLATE = """Today's date: {today}  (use this to resolve relative dates like "Thursday" or "next week")

Classify this message into one of these intents:

{available_skills}

Return ONLY valid JSON matching this schema exactly:
{{
  "intent": "<intent_name>",
  "confidence": <0.0-1.0>,
  "params": {{
    // intent-specific extracted fields — empty object if none
  }}
}}

Intent-specific param examples:
  gmail_read:     {{"action": "list" | "read" | "mark_read" | "labels", "query": "is:unread", "max_results": 10, "msg_id": ""}}
  gmail_send:     {{"to": "sarah@co.com", "subject": "Re: meeting", "body_hint": "I'll be 10 min late"}}
  gmail_reply:    {{"msg_id": "abc123", "body_hint": "sounds good, see you then"}}
  calendar_read:  {{"period": "today" | "tomorrow" | "this week" | "next week"}}
  calendar_write: {{"title": "Sprint review", "date": "2026-03-05", "time": "14:00", "duration_min": 60, "description": "", "timezone": "America/New_York", "attendees": ["person@example.com"]}}
  github_read:    {{"repo": "owner/name", "resource": "issues" | "prs" | "notifications"}}
  github_write:   {{"repo": "owner/name", "action": "create_issue", "title": "...", "body": "..."}}
  smart_home:     {{"action": "turn_on" | "turn_off" | "toggle" | "set" | "status", "entity": "light.living_room", "value": null}}
  n8n_execute:    {{"workflow": "daily_brief", "payload": {{}}}}
  n8n_manage:     {{"action": "list" | "get" | "create" | "activate" | "deactivate" | "delete", "workflow_id": "", "name": ""}}
  cicd_read:      {{"action": "list_workflows" | "list_runs" | "get_run", "repo": "owner/name", "workflow_id": "", "run_id": ""}}
  cicd_trigger:   {{"repo": "owner/name", "workflow_id": "deploy.yml", "ref": "main", "inputs": {{}}}}
  contacts_read:  {{"action": "search" | "list" | "lookup_email", "query": "Laura", "email": ""}}
  contacts_write: {{"action": "add" | "update" | "delete", "name": "Laura Smith", "email": "laura@co.com", "phone": "+12125551234", "company": "", "id": ""}}
  whatsapp_read:  {{"action": "list" | "get", "to": "+12125551234", "limit": 20}}
  whatsapp_send:  {{"to": "+12125551234", "body": "Hey, are we still on for tomorrow?"}}
  ionos_cloud:    {{"action": "list_datacenters" | "list_servers" | "create_datacenter" | "start_server" | "stop_server" | "ssh_exec" | "deploy_docker", "datacenter_id": "", "server_id": "", "name": "", "location": "de/fra", "host": "1.2.3.4", "command": "uptime"}}
  ionos_dns:      {{"action": "list_zones" | "list_records" | "upsert_record" | "delete_record", "zone_name": "example.com", "name": "www", "type": "A", "content": "1.2.3.4", "ttl": 3600}}
  repo_read:      {{"action": "status" | "diff" | "list_files" | "read_file", "path": "app/main.py"}}
  repo_write:     {{"action": "write_file" | "patch_file", "path": "app/main.py", "content": "...", "old": "...", "new": "..."}}
  repo_commit:    {{"action": "commit" | "push" | "commit_push", "message": "Fix calendar timezone bug", "push": true}}
  sentry_read:    {{"action": "list" | "get" | "db", "project": "", "query": "is:unresolved", "issue_id": "", "limit": 20}}
  sentry_manage:  {{"action": "resolve" | "ignore" | "assign" | "comment", "issue_id": "123456", "assignee": "user@co.com", "text": "looking into this"}}
  skill_discover: {{}}
  chat:           {{}}

IMPORTANT for calendar_write: "date" must always be an absolute ISO date (YYYY-MM-DD).
Never output day names like "Thursday" — resolve them using today's date above.

Message: {message}"""

_DEFAULT_SKILLS = """gmail_read      — read, check, or search Gmail inbox; read a specific email; mark as read
gmail_send      — compose, send, or draft an email
gmail_reply     — reply to a specific email in-thread
calendar_read   — check schedule, events, or availability
calendar_write  — create, update, reschedule, or cancel a calendar event
github_read     — check issues, PRs, notifications, or repo info
github_write    — create an issue, comment on a PR, close an issue
smart_home      — control or query a smart home device (lights, thermostat, locks, etc.)
n8n_execute     — run a specific n8n workflow by name
n8n_manage      — list, create, activate, or delete n8n workflows
cicd_read       — check CI/CD pipelines: list GitHub Actions workflows, view run status
cicd_trigger    — trigger a GitHub Actions workflow manually
contacts_read   — search or look up a contact by name or email in the address book
contacts_write  — add, update, or delete a contact in the address book
whatsapp_read   — read or check recent WhatsApp messages
whatsapp_send   — send a WhatsApp message to a contact or number
ionos_cloud     — manage IONOS cloud: datacenters, servers (spin up/down), SSH, deploy apps
ionos_dns       — manage IONOS DNS zones and records (A, CNAME, MX, TXT, etc.)
repo_read       — read, list, diff, or check status of the Brain's own codebase/files
repo_write      — create or edit a file in the Brain's codebase
repo_commit     — commit and/or push changes in the Brain's repository to GitHub
sentry_read     — list, search, or inspect Sentry error issues; show recent errors
sentry_manage   — resolve, ignore, assign, or comment on a Sentry issue
skill_discover  — when no skill exists for a task, analyze the gap and propose a new skill
chat            — anything else: analysis, writing, code, questions, conversation"""


# ── Classifier ────────────────────────────────────────────────────────────────

class IntentClassifier:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def classify(self, message: str, available_skills: str = "") -> dict:
        """
        Classify a message and return a dict with keys:
          intent (str), confidence (float), params (dict)
        Falls back to {"intent": "chat", "confidence": 0.5, "params": {}} on any error.
        """
        try:
            skill_list = available_skills.strip() if available_skills.strip() else _DEFAULT_SKILLS
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        message=message,
                        available_skills=skill_list,
                        today=_date.today().isoformat(),
                    ),
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
