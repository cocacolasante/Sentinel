"""
WhatsApp skills — read and send WhatsApp messages via Twilio.

Intents:
  whatsapp_read — list or check recent WhatsApp messages
  whatsapp_send — send a WhatsApp message
"""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class WhatsAppReadSkill(BaseSkill):
    name = "whatsapp_read"
    description = "Read or check recent WhatsApp messages"
    trigger_intents = ["whatsapp_read"]
    approval_category = ApprovalCategory.NONE

    def is_available(self) -> bool:
        from app.integrations.whatsapp import WhatsAppClient

        return WhatsAppClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.whatsapp import WhatsAppClient

        client = WhatsAppClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[WhatsApp not configured — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM in .env]",
                skill_name=self.name,
            )

        action = params.get("action", "list")
        sid = params.get("sid", "")

        if action == "get" and sid:
            msg = await client.get_message(sid)
            return SkillResult(context_data=json.dumps(msg, indent=2), skill_name=self.name)

        to = params.get("to", params.get("contact", ""))
        limit = int(params.get("limit", 20))
        msgs = await client.list_messages(to=to or None, limit=limit)
        if not msgs:
            return SkillResult(
                context_data="[No WhatsApp messages found]",
                skill_name=self.name,
            )
        return SkillResult(context_data=json.dumps(msgs, indent=2), skill_name=self.name)


class WhatsAppSendSkill(BaseSkill):
    name = "whatsapp_send"
    description = "Send a WhatsApp message to a contact or phone number"
    trigger_intents = ["whatsapp_send"]
    requires_confirmation = True
    approval_category = ApprovalCategory.STANDARD

    def is_available(self) -> bool:
        from app.integrations.whatsapp import WhatsAppClient

        return WhatsAppClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.whatsapp import WhatsAppClient

        if not WhatsAppClient().is_configured():
            return SkillResult(context_data="[WhatsApp not configured]", skill_name=self.name)

        to = params.get("to", "")
        body = params.get("body", params.get("message", ""))

        if not to:
            return SkillResult(
                context_data="[whatsapp_send requires a 'to' phone number or contact name]",
                skill_name=self.name,
            )

        pending = {
            "intent": "whatsapp_send",
            "action": "send_whatsapp",
            "params": params,
            "original": original_message,
        }
        context = (
            f"Show the user this WhatsApp message and ask them to confirm:\n\n"
            f"**To:** {to}\n"
            f"**Message:** {body or '(will be drafted from: ' + original_message + ')'}\n\n"
            "Reply **confirm** to send or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
