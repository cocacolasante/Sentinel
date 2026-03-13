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
        "Read Google Calendar events: list upcoming events, search by date range, check what's "
        "scheduled, get event details including attendees and location. Use when Anthony says "
        "'what's on my calendar', 'any meetings today/this week', 'show my schedule', "
        "'when is my next meeting', 'am I free on [date]', or 'check calendar for [date]'. "
        "NOT for: creating calendar events (use calendar_write) or email invites (use gmail_send)."
    )
    trigger_intents = ["calendar_read"]

    def is_available(self) -> bool:
        from app.integrations.google_calendar import CalendarClient

        return CalendarClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.google_calendar import get_calendar_client
        from app.config import get_settings

        account = params.get("account")  # None = all accounts
        period = params.get("period", "this week")

        if account:
            # Single account requested
            client = get_calendar_client(account_name=account)
            if not client.is_configured():
                return SkillResult(
                    context_data=f"[Google Calendar not configured for account '{account}']",
                    skill_name=self.name,
                    is_error=True,
                    needs_config=True,
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
                    is_error=True,
                    needs_config=True,
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
        "Create or update Google Calendar events: schedule meetings, add reminders, set recurring "
        "events, add attendees and video links. Use when Anthony says 'schedule a meeting', "
        "'create calendar event', 'block time on [date]', 'add event for [title] at [time]', "
        "'set up a call with [person]', or 'remind me about [thing] on [date]'. "
        "NOT for: reading calendar (use calendar_read) or sending email invites separately."
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
        client = get_calendar_client(account_name=account)

        if not client.is_configured():
            return SkillResult(
                context_data="[Google Calendar not configured — GOOGLE_REFRESH_TOKEN missing in .env]",
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        pending = {
            "intent": "calendar_write",
            "action": "create_calendar_event",
            "params": params,
            "original": original_message,
        }
        title = params.get("title", "New Event")
        date = params.get("date", "TBD")
        time_str = params.get("time", "TBD")
        duration = params.get("duration_min", 60)
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
