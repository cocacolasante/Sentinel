"""
Google Calendar Integration

Operations:
  list_events(period)           — fetch upcoming events ("today", "this week", etc.)
  create_event(params)          — create a new calendar event
  find_free_slots(date, hours)  — find available time blocks on a given date
  update_event(event_id, patch) — update an existing event
  delete_event(event_id)        — delete an event

Auth: same Google OAuth refresh token as Gmail.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

_PERIOD_DAYS: dict[str, int] = {
    "today":     0,
    "tomorrow":  1,
    "this week": 6,
    "next week": 13,
    "next 7 days": 6,
    "next 30 days": 29,
}


class CalendarClient:
    def __init__(self) -> None:
        self._service = None

    def is_configured(self) -> bool:
        return bool(
            settings.google_client_id
            and settings.google_client_secret
            and settings.google_refresh_token
        )

    def _build_service(self):
        if self._service is not None:
            return self._service
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=settings.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=_SCOPES,
        )
        creds.refresh(Request())
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    # ── Sync internals ────────────────────────────────────────────────────────

    def _list_events_sync(self, period: str, calendar_id: str) -> list[dict]:
        svc  = self._build_service()
        now  = datetime.now(tz=timezone.utc)
        days = _PERIOD_DAYS.get(period.lower(), 6)
        end  = now + timedelta(days=days + 1)

        result = svc.events().list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            maxResults=25,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = []
        for e in result.get("items", []):
            start = e.get("start", {})
            end_t = e.get("end", {})
            events.append({
                "id":          e.get("id"),
                "title":       e.get("summary", "(no title)"),
                "description": e.get("description", ""),
                "location":    e.get("location", ""),
                "start":       start.get("dateTime", start.get("date", "")),
                "end":         end_t.get("dateTime", end_t.get("date", "")),
                "attendees":   [a.get("email") for a in e.get("attendees", [])],
            })
        return events

    def _create_event_sync(self, params: dict, calendar_id: str) -> dict:
        svc = self._build_service()
        # Parse date + time from params
        date     = params.get("date", "")
        time_str = params.get("time", "09:00")
        duration = int(params.get("duration_min", 60))

        if date:
            try:
                start_dt = datetime.fromisoformat(f"{date}T{time_str}:00")
            except ValueError:
                start_dt = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        else:
            start_dt = datetime.now(tz=timezone.utc) + timedelta(hours=1)

        end_dt = start_dt + timedelta(minutes=duration)

        body = {
            "summary":     params.get("title", "New Event"),
            "description": params.get("description", ""),
            "location":    params.get("location", ""),
            "start":       {"dateTime": start_dt.isoformat(), "timeZone": settings.timezone},
            "end":         {"dateTime": end_dt.isoformat(),   "timeZone": settings.timezone},
        }
        if "attendees" in params:
            body["attendees"] = [{"email": e} for e in params["attendees"]]

        created = svc.events().insert(calendarId=calendar_id, body=body).execute()
        return {
            "id":    created.get("id"),
            "title": created.get("summary"),
            "start": created.get("start", {}).get("dateTime"),
            "link":  created.get("htmlLink"),
        }

    def _find_free_slots_sync(self, date: str, duration_min: int, calendar_id: str) -> list[dict]:
        svc = self._build_service()
        try:
            day_start = datetime.fromisoformat(f"{date}T08:00:00")
            day_end   = datetime.fromisoformat(f"{date}T20:00:00")
        except ValueError:
            return []

        fb = svc.freebusy().query(body={
            "timeMin": day_start.isoformat() + "Z",
            "timeMax": day_end.isoformat() + "Z",
            "items":   [{"id": calendar_id}],
        }).execute()

        busy_blocks = [
            (b["start"], b["end"])
            for b in fb.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        ]

        # Simple slot finder — 30-min grid
        free_slots = []
        cursor = day_start
        while cursor + timedelta(minutes=duration_min) <= day_end:
            slot_end = cursor + timedelta(minutes=duration_min)
            slot_start_str = cursor.isoformat() + "Z"
            slot_end_str   = slot_end.isoformat() + "Z"
            overlap = any(
                b_start < slot_end_str and b_end > slot_start_str
                for b_start, b_end in busy_blocks
            )
            if not overlap:
                free_slots.append({
                    "start": cursor.strftime("%H:%M"),
                    "end":   slot_end.strftime("%H:%M"),
                })
            cursor += timedelta(minutes=30)

        return free_slots[:8]  # return first 8 slots

    # ── Public async API ──────────────────────────────────────────────────────

    async def list_events(
        self,
        period: str = "this week",
        calendar_id: str | None = None,
    ) -> list[dict]:
        cal_id = calendar_id or settings.google_calendar_id
        return await asyncio.to_thread(self._list_events_sync, period, cal_id)

    async def create_event(
        self,
        params: dict,
        calendar_id: str | None = None,
    ) -> dict:
        cal_id = calendar_id or settings.google_calendar_id
        return await asyncio.to_thread(self._create_event_sync, params, cal_id)

    async def find_free_slots(
        self,
        date: str,
        duration_min: int = 60,
        calendar_id: str | None = None,
    ) -> list[dict]:
        cal_id = calendar_id or settings.google_calendar_id
        return await asyncio.to_thread(self._find_free_slots_sync, date, duration_min, cal_id)
