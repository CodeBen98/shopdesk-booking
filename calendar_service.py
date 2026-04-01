"""
calendar_service.py — Google Calendar integration for ShopDesk AI Receptionist.

Uses OAuth2 (user credentials) to read/write the shop calendar.
All Google API calls are synchronous under the hood; async wrappers use run_in_executor.
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta, time
from typing import List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
APPOINTMENT_DURATION_MINUTES = 30
TIMEZONE = "America/New_York"

TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

import base64
import tempfile

def _write_google_files_from_env():
    """If Google credentials are stored as base64 env vars, write them to temp files."""
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_B64")
    token_b64 = os.getenv("GOOGLE_TOKEN_B64")
    if creds_b64 and not os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, "w") as f:
            f.write(base64.b64decode(creds_b64).decode())
        logger.info("Wrote credentials.json from GOOGLE_CREDENTIALS_B64")
    if token_b64 and not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(base64.b64decode(token_b64).decode())
        logger.info("Wrote token.json from GOOGLE_TOKEN_B64")

# Business hours: weekday index → (open_hour, close_hour) or None if closed
# 535 Auto Care: Mon-Fri 9am-5pm, closed weekends
BUSINESS_HOURS = {
    0: (9, 17),   # Monday
    1: (9, 17),   # Tuesday
    2: (9, 17),   # Wednesday
    3: (9, 17),   # Thursday
    4: (9, 17),   # Friday
    5: None,      # Saturday — closed
    6: None,      # Sunday — closed
}

SHOP_TIMEZONE = ZoneInfo(TIMEZONE)


class GoogleCalendarService:
    """Wraps Google Calendar API with business-hours slot logic."""

    def __init__(self):
        _write_google_files_from_env()
        self._service = self._build_service()

    def _build_service(self):
        """Authenticate via OAuth2 and build the Google Calendar API client."""
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = None

        # 1. Load existing token if present
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        # 2. Refresh or run the OAuth flow if credentials are missing/expired
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)

            # 3. Save token for future runs
            with open(TOKEN_FILE, "w") as token_file:
                token_file.write(creds.to_json())
            logger.info(f"OAuth token saved to {TOKEN_FILE}")

        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    # ── Async public API ──────────────────────────────────────────────────────

    async def get_available_slots(self, num_days: int = 3) -> List[datetime]:
        """Return available 30-min appointment slots over the next num_days business days."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_available_slots_sync, num_days)

    async def book_appointment(
        self,
        name: str,
        phone: str,
        vehicle: str,
        issue: str,
        slot_datetime: Optional[datetime],
    ) -> str:
        """Create a calendar event; return the HTML event link."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._book_appointment_sync, name, phone, vehicle, issue, slot_datetime
        )

    # ── Sync implementations ──────────────────────────────────────────────────

    def _get_available_slots_sync(self, num_days: int = 3) -> List[datetime]:
        """Find available 30-min slots across the next business days."""
        try:
            service = self._service
        except Exception as e:
            logger.error(f"Calendar service unavailable: {e} — using fallback slots")
            return self._fallback_slots(num_days)

        slots: List[datetime] = []
        now = datetime.now()
        current_date = now.date()
        days_scanned = 0
        business_days_found = 0

        while business_days_found < num_days and days_scanned < num_days * 5:
            weekday = current_date.weekday()
            hours = BUSINESS_HOURS.get(weekday)

            if hours:
                business_days_found += 1
                open_hour, close_hour = hours

                # Fetch existing events for this day
                try:
                    day_start = datetime.combine(current_date, time(0, 0)).isoformat() + "Z"
                    day_end = datetime.combine(current_date, time(23, 59)).isoformat() + "Z"
                    result = service.events().list(
                        calendarId=CALENDAR_ID,
                        timeMin=day_start,
                        timeMax=day_end,
                        singleEvents=True,
                        orderBy="startTime",
                    ).execute()
                    existing = result.get("items", [])
                except Exception as e:
                    logger.warning(f"Could not fetch events for {current_date}: {e}")
                    existing = []

                # Walk 30-min slots during business hours
                slot_dt = datetime.combine(current_date, time(open_hour, 0))

                # Skip past times on today (require at least 2h notice)
                if current_date == now.date():
                    min_start = now + timedelta(hours=2)
                    # Round up to next 30-min boundary
                    minutes = min_start.minute
                    if minutes % 30 != 0:
                        min_start += timedelta(minutes=30 - (minutes % 30))
                    min_start = min_start.replace(second=0, microsecond=0)
                    slot_dt = max(slot_dt, min_start)

                while (slot_dt.hour < close_hour or
                       (slot_dt.hour == close_hour and slot_dt.minute == 0)) and \
                      slot_dt < datetime.combine(current_date, time(close_hour, 0)):
                    slot_end = slot_dt + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                    if not self._has_conflict(slot_dt, slot_end, existing):
                        slots.append(slot_dt)
                    slot_dt += timedelta(minutes=30)

            current_date += timedelta(days=1)
            days_scanned += 1

        return slots

    def _has_conflict(
        self, start: datetime, end: datetime, events: list
    ) -> bool:
        """Return True if the proposed slot overlaps any existing event."""
        for event in events:
            ev_start_str = event.get("start", {}).get("dateTime", "")
            ev_end_str = event.get("end", {}).get("dateTime", "")
            if not ev_start_str or not ev_end_str:
                continue
            try:
                ev_start = datetime.fromisoformat(ev_start_str).replace(tzinfo=None)
                ev_end = datetime.fromisoformat(ev_end_str).replace(tzinfo=None)
                if start < ev_end and end > ev_start:
                    return True
            except (ValueError, TypeError):
                continue
        return False

    def _fallback_slots(self, num_days: int) -> List[datetime]:
        """Generate plausible 30-min slots when Calendar API is unreachable."""
        slots: List[datetime] = []
        now = datetime.now()
        # Start 2 hours from now, rounded up to next 30-min boundary
        candidate = now + timedelta(hours=2)
        minutes = candidate.minute
        if minutes % 30 != 0:
            candidate += timedelta(minutes=30 - (minutes % 30))
        candidate = candidate.replace(second=0, microsecond=0)

        business_days_seen = set()
        while len(business_days_seen) < num_days or len(slots) < num_days * 2:
            weekday = candidate.weekday()
            hours = BUSINESS_HOURS.get(weekday)
            if hours:
                open_hour, close_hour = hours
                if open_hour <= candidate.hour < close_hour:
                    slots.append(candidate)
                    business_days_seen.add(candidate.date())
                    if len(slots) >= num_days * 4:
                        break
            candidate += timedelta(minutes=30)
            # Jump to next morning if past business hours
            if candidate.hour >= 18:
                candidate = datetime.combine(
                    candidate.date() + timedelta(days=1), time(8, 0)
                )

        return slots

    def _book_appointment_sync(
        self,
        name: str,
        phone: str,
        vehicle: str,
        issue: str,
        slot_datetime: Optional[datetime],
    ) -> str:
        """Create calendar event; return event URL or fallback confirmation string."""
        tz = ZoneInfo(TIMEZONE)

        if not slot_datetime:
            slot_datetime = datetime.now(tz) + timedelta(days=1, hours=10)
            slot_datetime = slot_datetime.replace(minute=0, second=0, microsecond=0)

        # Make timezone-aware if naive (parsed datetime has no tz info)
        if slot_datetime.tzinfo is None:
            slot_datetime = slot_datetime.replace(tzinfo=tz)

        slot_end = slot_datetime + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        event_body = {
            "summary": f"ShopDesk: {issue} — {name} ({vehicle})",
            "description": (
                f"Customer: {name}\n"
                f"Phone: {phone}\n"
                f"Vehicle: {vehicle}\n"
                f"Issue / Service: {issue}\n"
                f"\nBooked by ShopDesk AI Receptionist"
            ),
            "start": {"dateTime": slot_datetime.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": slot_end.isoformat(), "timeZone": TIMEZONE},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 24 * 60},
                    {"method": "popup", "minutes": 60},
                ],
            },
        }

        try:
            created = self._service.events().insert(
                calendarId=CALENDAR_ID, body=event_body
            ).execute()
            link = created.get("htmlLink", "")
            logger.info(f"Calendar event created: {link}")
            return link
        except Exception as e:
            logger.error(f"Failed to create calendar event: {e}")
            return slot_datetime.strftime("Appointment on %A, %B %d at %I:%M %p")
