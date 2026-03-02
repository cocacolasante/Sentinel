"""Calendar skills — read schedule and create/update events."""

from __future__ import annotations

import json

from app.skills.base import BaseSkill, SkillResult


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

    def is_available(self) -> bool:
        from app.integrations.google_calendar import CalendarClient
        return CalendarClient().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.google_calendar import CalendarClient
        client = CalendarClient()
        if not client.is_configured():
            return SkillResult(context_data="[Google Calendar not configured]", skill_name=self.name)
        result = await client.create_event(params)
        return SkillResult(context_data=json.dumps(result, indent=2), skill_name=self.name)
