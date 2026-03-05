"""Gmail skills — read inbox, read full email, reply, send/draft emails.

Multi-account: pass account="work" (or whatever name you set in .env) to
target a specific Gmail account.  Omitting it uses the primary account.
"""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


def _account_label(account_name: str | None) -> str:
    return f" ({account_name})" if account_name else ""


class GmailReadSkill(BaseSkill):
    name = "gmail_read"
    description = (
        "Read, check, search Gmail inbox — list emails, read full message, mark as read. "
        "Supports multiple accounts via the 'account' param (e.g. account='work')."
    )
    trigger_intents = ["gmail_read"]

    def is_available(self) -> bool:
        from app.integrations.gmail import GmailClient

        return GmailClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.gmail import get_gmail_client

        account = params.get("account")
        client = get_gmail_client(account_name=account)

        if not client.is_configured():
            return SkillResult(
                context_data="[Gmail not configured — GOOGLE_REFRESH_TOKEN missing in .env]",
                skill_name=self.name,
            )

        label = _account_label(client.account_name)
        action = params.get("action", "list")
        msg_id = params.get("msg_id", "")

        if action == "read" and msg_id:
            email = await client.get_email(msg_id)
            await client.mark_read(msg_id)
            return SkillResult(
                context_data=json.dumps({"account": client.account_name, **email}, indent=2),
                skill_name=self.name,
            )

        if action == "mark_read" and msg_id:
            result = await client.mark_read(msg_id)
            return SkillResult(
                context_data=json.dumps(result),
                skill_name=self.name,
            )

        if action == "labels":
            labels = await client.list_labels()
            return SkillResult(
                context_data=json.dumps({"account": client.account_name, "labels": labels}, indent=2),
                skill_name=self.name,
            )

        # Default: list emails
        query = params.get("query", "is:unread")
        max_results = int(params.get("max_results", 10))
        emails = await client.list_emails(query=query, max_results=max_results)
        return SkillResult(
            context_data=json.dumps({"account": client.account_name, "emails": emails}, indent=2),
            skill_name=self.name,
        )


class GmailSendSkill(BaseSkill):
    name = "gmail_send"
    description = (
        "Compose, draft, or send an email via Gmail. "
        "Supports multiple accounts via the 'account' param (e.g. account='work')."
    )
    trigger_intents = ["gmail_send"]
    requires_confirmation = True
    approval_category = ApprovalCategory.STANDARD

    def is_available(self) -> bool:
        from app.integrations.gmail import GmailClient

        return GmailClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.gmail import get_gmail_client

        account = params.get("account")
        client = get_gmail_client(account_name=account)

        if not client.is_configured():
            return SkillResult(context_data="[Gmail not configured]", skill_name=self.name)

        pending = {
            "intent": "gmail_send",
            "action": "send_email",
            "params": params,
            "original": original_message,
        }
        to = params.get("to", "unknown")
        subject = params.get("subject", "")
        context = (
            f"Draft an email based on the user's request. "
            f"Account: {client.account_name}. "
            f"Recipient: {to}. "
            f"Subject hint: {subject}. "
            f"Content hint: {params.get('body_hint', original_message)}. "
            "Show the full draft (Account, To, Subject, Body) formatted clearly. "
            "Explain that the user must confirm before it is sent."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )


class GmailReplySkill(BaseSkill):
    name = "gmail_reply"
    description = (
        "Reply to a specific email in-thread via Gmail. "
        "Supports multiple accounts via the 'account' param (e.g. account='work')."
    )
    trigger_intents = ["gmail_reply"]
    requires_confirmation = True
    approval_category = ApprovalCategory.STANDARD

    def is_available(self) -> bool:
        from app.integrations.gmail import GmailClient

        return GmailClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.gmail import get_gmail_client

        account = params.get("account")
        client = get_gmail_client(account_name=account)

        if not client.is_configured():
            return SkillResult(context_data="[Gmail not configured]", skill_name=self.name)

        msg_id = params.get("msg_id", "")
        if not msg_id:
            return SkillResult(
                context_data="[gmail_reply requires a msg_id — ask the user which email to reply to]",
                skill_name=self.name,
            )

        try:
            email = await client.get_email(msg_id)
            from_addr = email.get("from", "?")
            subject = email.get("subject", "?")
        except Exception:
            from_addr = "?"
            subject = "?"

        pending = {
            "intent": "gmail_reply",
            "action": "reply_email",
            "params": params,
            "original": original_message,
        }
        context = (
            f"Draft a reply to this email and show it to the user for confirmation:\n"
            f"  Account: {client.account_name}\n"
            f"  From: {from_addr}\n"
            f"  Subject: {subject}\n"
            f"  Reply hint: {params.get('body_hint', original_message)}\n\n"
            "Show the full reply body. Explain the user must confirm before it is sent."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
