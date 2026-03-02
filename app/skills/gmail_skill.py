"""Gmail skills — read inbox and send/draft emails."""

from __future__ import annotations

import json

from app.skills.base import BaseSkill, SkillResult


class GmailReadSkill(BaseSkill):
    name = "gmail_read"
    description = "Read, check, or search Gmail inbox"
    trigger_intents = ["gmail_read"]

    def is_available(self) -> bool:
        from app.integrations.gmail import GmailClient
        return GmailClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.gmail import GmailClient
        client = GmailClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[Gmail not configured — GOOGLE_REFRESH_TOKEN missing in .env]",
                skill_name=self.name,
            )
        query       = params.get("query", "is:unread")
        max_results = int(params.get("max_results", 10))
        emails = await client.list_emails(query=query, max_results=max_results)
        return SkillResult(
            context_data=json.dumps(emails, indent=2),
            skill_name=self.name,
        )


class GmailSendSkill(BaseSkill):
    name = "gmail_send"
    description = "Compose, draft, or send an email via Gmail"
    trigger_intents = ["gmail_send"]
    requires_confirmation = True

    def is_available(self) -> bool:
        from app.integrations.gmail import GmailClient
        return GmailClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.gmail import GmailClient
        if not GmailClient().is_configured():
            return SkillResult(context_data="[Gmail not configured]", skill_name=self.name)

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
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
