"""
Contacts skill — address book CRUD.

Intents:
  contacts_read  — search, list, or look up a contact
  contacts_write — add, update, or delete a contact
"""

from __future__ import annotations

import json
import logging

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

logger = logging.getLogger(__name__)


class ContactsReadSkill(BaseSkill):
    name = "contacts_read"
    description = "Search the address book and look up contacts by name, email, or company. Use when Anthony says 'find contact', 'look up [name]', 'what\\'s [name]\\'s email', 'list contacts', or 'who is [name]'. NOT for: adding/updating contacts (use contacts_write), or searching emails in Gmail."
    trigger_intents = ["contacts_read"]
    approval_category = ApprovalCategory.NONE

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.contacts import ContactsClient

        client = ContactsClient()

        action = params.get("action", "search")
        query = params.get("query", params.get("name", ""))
        email = params.get("email", "")

        if action == "list" or (not query and not email):
            try:
                contacts = await client.get_all(limit=50)
            except Exception as exc:
                logger.exception("ContactsReadSkill get_all: %s", exc)
                return SkillResult(
                    context_data=f"[Contacts error listing all contacts: {exc}]",
                    skill_name=self.name,
                    is_error=True,
                )
            return SkillResult(
                context_data=json.dumps(contacts, indent=2, default=str),
                skill_name=self.name,
            )

        if action == "lookup_email" or email:
            try:
                contact = await client.get_by_email(email)
            except Exception as exc:
                logger.exception("ContactsReadSkill get_by_email email=%s: %s", email, exc)
                return SkillResult(
                    context_data=f"[Contacts error looking up email '{email}': {exc}]",
                    skill_name=self.name,
                    is_error=True,
                )
            return SkillResult(
                context_data=json.dumps(contact, indent=2, default=str)
                if contact
                else f"[No contact with email {email}]",
                skill_name=self.name,
            )

        # Default: search by name or general query
        try:
            results = await client.search(query, limit=10)
        except Exception as exc:
            logger.exception("ContactsReadSkill search query=%s: %s", query, exc)
            return SkillResult(
                context_data=f"[Contacts error searching for '{query}': {exc}]",
                skill_name=self.name,
                is_error=True,
            )
        if not results:
            return SkillResult(
                context_data=f"[No contacts found matching '{query}']",
                skill_name=self.name,
            )
        return SkillResult(
            context_data=json.dumps(results, indent=2, default=str),
            skill_name=self.name,
        )


class ContactsWriteSkill(BaseSkill):
    name = "contacts_write"
    description = "Add, update, or delete contacts in the address book. Use when Anthony says 'add contact', 'save [name]\\'s details', 'update [name]\\'s phone', or 'delete contact'. Requires confirmation before executing. NOT for: reading/searching contacts (use contacts_read)."
    trigger_intents = ["contacts_write"]
    requires_confirmation = True
    approval_category = ApprovalCategory.STANDARD

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        action = params.get("action", "add")
        name = params.get("name", "")

        pending = {
            "intent": "contacts_write",
            "action": action,
            "params": params,
            "original": original_message,
        }

        if action == "add":
            if not name:
                return SkillResult(
                    context_data="[contacts_write requires at least a name to add a contact]",
                    skill_name=self.name,
                )
            fields = {
                k: params.get(k, "")
                for k in ("email", "phone", "whatsapp", "company", "github", "slack_id", "tags", "notes")
            }
            field_summary = "\n".join(f"  {k}: {v}" for k, v in fields.items() if v)
            context = (
                f"Show the user the contact that is about to be added and ask them to confirm:\n\n"
                f"  Name: {name}\n"
                f"{field_summary}\n\n"
                "Reply **confirm** to save or **cancel** to abort."
            )

        elif action in ("update", "delete"):
            contact_id = params.get("id", "")
            context = (
                f"Show the user that the contact (ID: {contact_id}, Name: {name}) "
                f"is about to be **{action}d**.\n"
                f"Fields to update: {json.dumps({k: v for k, v in params.items() if k not in ('action', 'id', 'name')})}\n\n"
                "Reply **confirm** to proceed or **cancel** to abort."
            )

        else:
            return SkillResult(
                context_data=f"[Unknown contacts action: {action}]",
                skill_name=self.name,
            )

        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
