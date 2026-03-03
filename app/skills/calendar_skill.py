"""Calendar skills — read schedule and create/update events.

Multi-account: pass account="work" (or whatever name you set in .env) to
target a specific Google Calendar account.  Omitting the account param causes
CalendarReadSkill to query ALL configured accounts and merge the results.
"""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class CalendarReadSkill(BaseSkill):
    name = "calendar_read"
    description = (
        "Check schedule, events, or availability in Google Calendar. "
        "Reads ALL configured calendars by default, or a specific one via 'account' param."
    )
    trigger_intents = ["calendar_read"]

    def is_available(self) -> bool:
        from app.integrations.google_calendar import CalendarClient
        return CalendarClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.google_calendar import get_calendar_client
        from app.config import get_settings

        account = params.get("account")  # None = all accounts
        period  = params.get("period", "this week")

        if account:
            # Single account requested
            client = get_calendar_client(account_name=account)
            if not client.is_configured():
                return SkillResult(
                    context_data=f"[Google Calendar not configured for account '{account}']",
                    skill_name=self.name,
                )
            events = await client.list_events(period=period)
            result = {client.account_name: events}
        else:
            # All configured accounts
            accounts = get_settings().google_accounts
            if not accounts:
                return SkillResult(
                    context_data="[Google Calendar not configured — GOOGLE_REFRESH_TOKEN missing]",
                    skill_name=self.name,
                )
            result = {}
            for acc in accounts:
                client = get_calendar_client(account_name=acc["name"])
                if client.is_configured():
                    try:
                        result[acc["name"]] = await client.list_events(period=period)
                    except Exception as exc:
                        result[acc["name"]] = f"[Error: {exc}]"

        return SkillResult(
            context_data=json.dumps(result, indent=2),
            skill_name=self.name,
        )


class CalendarWriteSkill(BaseSkill):
    name = "calendar_write"
    description = (
        "Create, update, reschedule, or cancel a Google Calendar event. "
        "Use the 'account' param to choose which calendar to write to."
    )
    trigger_intents = ["calendar_write"]
    requires_confirmation = True
    approval_category = ApprovalCategory.STANDARD

    def is_available(self) -> bool:
        from app.integrations.google_calendar import CalendarClient
        return CalendarClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.google_calendar import get_calendar_client
        account = params.get("account")
        client  = get_calendar_client(account_name=account)

        if not client.is_configured():
            return SkillResult(
                context_data="[Google Calendar not configured — GOOGLE_REFRESH_TOKEN missing in .env]",
                skill_name=self.name,
            )

        pending = {
            "intent":   "calendar_write",
            "action":   "create_calendar_event",
            "params":   params,
            "original": original_message,
        }
        title     = params.get("title", "New Event")
        date      = params.get("date", "TBD")
        time_str  = params.get("time", "TBD")
        duration  = params.get("duration_min", 60)
        attendees = [e for e in params.get("attendees", []) if "@" in str(e)]
        context = (
            f"Show the user the calendar event you are about to create:\n"
            f"  Calendar: {client.account_name}\n"
            f"  Title: {title}\n"
            f"  Date: {date}\n"
            f"  Time: {time_str} ({duration} min)\n"
            f"  Description: {params.get('description', '')}\n"
            f"  Location: {params.get('location', '')}\n"
            + (f"  Invite: {', '.join(attendees)}\n" if attendees else "")
            + ("  (A personal Gmail invite will also be sent to each attendee.)\n" if attendees else "")
            + "Format it clearly and ask the user to reply **confirm** to add it or **cancel** to abort."
        )
        return SkillResult(
            context_data=context,
            pending_action=pending,
            skill_name=self.name,
        )
