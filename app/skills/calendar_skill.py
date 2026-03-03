"""Calendar skills — read schedule and create/update events."""

from __future__ import annotations

import json

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


class CalendarReadSkill(BaseSkill):
    name = "calendar_read"
    description = "Check schedule, events, or availability in Google Calendar"
    trigger_intents = ["calendar_read"]

    def is_available(self) -> bool:
        from app.integrations.google_calendar import CalendarClient
        return CalendarClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.google_calendar import CalendarClient
        client = CalendarClient()
        if not client.is_configured():
            return SkillResult(
                context_data="[Google Calendar not configured — GOOGLE_REFRESH_TOKEN missing]",
                skill_name=self.name,
            )
        period = params.get("period", "this week")
        events = await client.list_events(period=period)
        return SkillResult(context_data=json.dumps(events, indent=2), skill_name=self.name)


class CalendarWriteSkill(BaseSkill):
    name = "calendar_write"
    description = "Create, update, reschedule, or cancel a Google Calendar event"
    trigger_intents = ["calendar_write"]
    requires_confirmation = True
    approval_category = ApprovalCategory.STANDARD

    def is_available(self) -> bool:
        from app.integrations.google_calendar import CalendarClient
        return CalendarClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.google_calendar import CalendarClient
        if not CalendarClient().is_configured():
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
