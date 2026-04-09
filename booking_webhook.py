"""
booking_webhook.py — ShopDesk booking tool webhook for ElevenLabs Conversational AI.

ElevenLabs calls POST /book when the AI agent wants to book an appointment.
Returns a confirmation string the AI reads back to the caller.

Run with: uvicorn booking_webhook:app --host 0.0.0.0 --port 8000
Expose with: ngrok http 8000
"""

import os
import logging
from datetime import datetime

import uvicorn
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from typing import Optional
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
# Log key type at startup for verification
try:
    import base64 as _b64, json as _json
    _payload = SUPABASE_SERVICE_KEY.split('.')[1] if SUPABASE_SERVICE_KEY else ''
    _payload += '=' * (4 - len(_payload) % 4)
    _key_role = _json.loads(_b64.b64decode(_payload)).get('role', 'unknown') if _payload else 'missing'
except Exception:
    _key_role = 'parse_error'
print(f"[STARTUP] SUPABASE key role: {_key_role} (len={len(SUPABASE_SERVICE_KEY)}) start={SUPABASE_SERVICE_KEY[:30] if SUPABASE_SERVICE_KEY else 'EMPTY'}")
DEFAULT_SHOP_ID = os.getenv("DEFAULT_SHOP_ID", "")  # fallback only

_shop_cache: dict = {}  # simple in-memory cache: assistant_id -> shop row

async def get_shop_by_assistant(assistant_id: str) -> dict:
    """Look up shop from Supabase by Vapi assistant ID. Returns shop dict or empty dict."""
    if not assistant_id:
        return await get_shop_by_id(DEFAULT_SHOP_ID)
    if assistant_id in _shop_cache:
        return _shop_cache[assistant_id]
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/shops?vapi_agent_id=eq.{assistant_id}&select=id,name,email,google_refresh_token,google_calendar_id",
                headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
                timeout=5,
            )
            if resp.status_code == 200 and resp.json():
                shop = resp.json()[0]
                _shop_cache[assistant_id] = shop
                return shop
    except Exception as e:
        logger.error(f"get_shop_by_assistant error: {e}")
    # Fall back to default shop ID
    return await get_shop_by_id(DEFAULT_SHOP_ID)

async def get_shop_by_id(shop_id: str) -> dict:
    """Look up shop by ID."""
    if not shop_id or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/shops?id=eq.{shop_id}&select=id,name,email,google_refresh_token,google_calendar_id",
                headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
                timeout=5,
            )
            if resp.status_code == 200 and resp.json():
                return resp.json()[0]
    except Exception as e:
        logger.error(f"get_shop_by_id error: {e}")
    return {}

from calendar_service import GoogleCalendarService
from sms_service import send_confirmation, send_owner_alert
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@email.shopdesk.site")

async def send_booking_email(to_email: str, shop_name: str, customer_name: str,
                              customer_phone: str, vehicle: str, service: str,
                              appointment_time: str, calendar_link: str = "") -> None:
    """Send booking notification email to shop owner via Resend API."""
    if not to_email or not RESEND_API_KEY:
        logger.warning("Email not configured — skipping booking email")
        return
    try:
        html = f"""<div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:24px">
<div style="background:#0b141e;padding:16px 24px;border-radius:8px 8px 0 0;border-bottom:3px solid #0F7695">
  <span style="font-size:18px;font-weight:700;color:#f4f5f7">ShopDesk</span>
</div>
<div style="background:#fff;padding:32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px">
  <h2 style="font-size:20px;font-weight:700;color:#111827;margin:0 0 4px">New Appointment Booked</h2>
  <p style="color:#6b7280;margin:0 0 24px;font-size:14px">{shop_name}</p>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#6b7280;width:120px">Customer</td><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#111827;font-weight:600">{customer_name}</td></tr>
    <tr><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#6b7280">Phone</td><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#111827">{customer_phone}</td></tr>
    <tr><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#6b7280">Vehicle</td><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#111827">{vehicle}</td></tr>
    <tr><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#6b7280">Service</td><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;color:#111827">{service}</td></tr>
    <tr><td style="padding:10px 0;color:#6b7280">Time</td><td style="padding:10px 0;color:#0F7695;font-weight:600">{appointment_time}</td></tr>
  </table>
  <div style="margin-top:24px">
    <a href="https://app.shopdesk.site/dashboard/bookings" style="background:#0F7695;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;display:inline-block">View in Dashboard</a>
  </div>
  {"<p style='margin-top:16px;font-size:13px;color:#6b7280'>💡 <a href=\"https://app.shopdesk.site/dashboard/settings\" style=\"color:#0F7695\">Connect Google Calendar</a> to automatically add bookings.</p>" if not calendar_link else ""}
</div>
</div>"""

        payload = {
            "from": f"ShopDesk <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"New Booking — {customer_name} — {service}",
            "html": html,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Booking email sent to {to_email}")
            else:
                logger.error(f"Resend email failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"send_booking_email error: {e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="ShopDesk Booking Webhook")

SHOP_NAME = os.getenv("SHOP_NAME", "Mike's Auto Repair")
SHOP_ADDRESS = os.getenv("SHOP_ADDRESS", "1234 Orange Ave, Orlando, FL 32801")
SHOP_PHONE = os.getenv("SHOP_PHONE", "(407) 555-0123")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")


class BookingRequest(BaseModel):
    name: str
    phone: str
    vehicle: str          # e.g. "2019 Honda Civic"
    issue: str            # e.g. "oil change"
    appointment_time: str # e.g. "Monday March 31 at 10:00 AM" — free-form from the AI


class VapiBookingRequest(BaseModel):
    """Vapi tool call payload — parameters are nested under 'parameters'."""
    customer_name: str
    customer_phone: str
    vehicle_year: Optional[str] = ""
    vehicle_make: Optional[str] = ""
    vehicle_model: Optional[str] = ""
    service: str
    appointment_time: str


async def save_booking_to_supabase(
    shop_id: str, customer_name: str, customer_phone: str,
    vehicle: str, service: str, slot_dt, appointment_time: str,
    calendar_link: str = "", vapi_call_id: str = ""
) -> None:
    """Write booking to Supabase bookings table. Never raises — failures are logged only."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not shop_id:
        logger.warning("Supabase env vars not set — skipping DB write")
        return
    try:
        # Ensure timezone-aware ISO format for correct storage
        if slot_dt:
            from zoneinfo import ZoneInfo
            EASTERN = ZoneInfo("America/New_York")
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=EASTERN)
            appt_iso = slot_dt.isoformat()
        else:
            appt_iso = appointment_time
        # Look up call record UUID from vapi_call_id to link booking → call
        call_uuid = None
        if vapi_call_id:
            try:
                call_lookup = await httpx.AsyncClient().get(
                    f"{SUPABASE_URL}/rest/v1/calls?vapi_call_id=eq.{vapi_call_id}&select=id",
                    headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
                    timeout=5,
                )
                rows = call_lookup.json()
                if rows:
                    call_uuid = rows[0].get("id")
            except Exception:
                pass

        from datetime import timezone
        consent_ts = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "shop_id": shop_id,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "vehicle": vehicle or None,
            "service": service or None,
            "appointment_at": appt_iso,
            "status": "scheduled",
            "sms_sent": False,
            "notes": f"Calendar: {calendar_link}" if calendar_link else None,
            "call_id": call_uuid,
            "sms_consent_given": True,
            "sms_consent_timestamp": consent_ts,
            "sms_consent_source": "phone_call_verbal",
            "sms_consent_call_id": vapi_call_id or None,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/bookings",
                json=payload,
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                timeout=10,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Booking saved to Supabase for {customer_name}")
            else:
                logger.error(f"Supabase booking insert failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"save_booking_to_supabase error: {e}")


@app.post("/book")
async def book_appointment(request: Request):
    """
    Universal booking endpoint — handles both Vapi and legacy ElevenLabs formats.
    Vapi sends: {"message": {"toolCalls": [{"id": "...", "function": {"name": "...", "arguments": "{...}"}}]}}
    Legacy sends: {"name": "...", "phone": "...", "vehicle": "...", "issue": "...", "appointment_time": "..."}
    """
    body = await request.json()
    logger.info(f"Booking request body: {str(body)[:300]}")

    # Detect Vapi format
    tool_calls = body.get("message", {}).get("toolCalls", [])
    is_vapi = bool(tool_calls)

    if is_vapi:
        tc = tool_calls[0]
        tool_call_id = tc.get("id", "")
        raw_args = tc.get("function", {}).get("arguments", "{}")
        if isinstance(raw_args, str):
            import json as _json
            args = _json.loads(raw_args)
        else:
            args = raw_args
        vehicle_str = f"{args.get('vehicle_year','')} {args.get('vehicle_make','')} {args.get('vehicle_model','')}".strip()
        name = args.get("customer_name", "")
        phone = args.get("customer_phone", "")
        issue = args.get("service", "")
        appointment_time = args.get("appointment_time", "")
        assistant_id = args.get("assistant_id", "")
        vapi_call_id = body.get("message", {}).get("call", {}).get("id", "")
    else:
        assistant_id = body.get("assistant_id", "")
        vapi_call_id = ""
        # Legacy ElevenLabs format
        name = body.get("name", "")
        phone = body.get("phone", "")
        vehicle_str = body.get("vehicle", "")
        issue = body.get("issue", "")
        appointment_time = body.get("appointment_time", "")
        tool_call_id = None

    slot_dt = _parse_appointment_time(appointment_time)

    # Look up shop — prefer query param shop_id (injected by provision), fallback to assistant_id
    query_shop_id = request.query_params.get("shop_id", "")
    if query_shop_id:
        shop_row = await get_shop_by_id(query_shop_id)
    else:
        shop_row = await get_shop_by_assistant(assistant_id)
    shop_id_resolved = shop_row.get("id") or DEFAULT_SHOP_ID

    # Book calendar event — use per-shop refresh token if available
    cal_link = None
    try:
        shop_refresh_token = shop_row.get("google_refresh_token")
        shop_calendar_id = shop_row.get("google_calendar_id") or CALENDAR_ID

        if not shop_refresh_token:
            # No calendar connected — skip calendar booking, notify owner via email instead
            logger.info("No Google refresh token for shop — skipping calendar, owner will be notified via email")
            cal_link = None
        else:
            # Use per-shop OAuth token
            google_client_id = os.getenv("GOOGLE_CLIENT_ID", "")
            google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
            cal = GoogleCalendarService(
                refresh_token=shop_refresh_token,
                client_id=google_client_id,
                client_secret=google_client_secret,
                shop_hours=shop_row.get("hours"),
            )
            cal_link = await cal.book_appointment(
                name=name,
                phone=phone,
                vehicle=vehicle_str or "Vehicle TBD",
                issue=issue,
                slot_datetime=slot_dt,
            )
            # Check if calendar failed after retries
            if cal_link and cal_link.startswith("CALENDAR_FAILED:"):
                logger.error(f"Calendar booking failed after retries, notifying owner")
                cal_link = None  # Don't save failed link
            else:
                logger.info(f"Calendar event created: {cal_link}")
    except Exception as e:
        logger.error(f"Calendar booking failed: {e}")

    # Write booking to Supabase
    try:
        await save_booking_to_supabase(
            shop_id=shop_id_resolved,
            customer_name=name,
            customer_phone=phone,
            vehicle=vehicle_str or "",
            service=issue,
            slot_dt=slot_dt,
            appointment_time=appointment_time,
            calendar_link=cal_link or "",
            vapi_call_id=vapi_call_id,
        )
    except Exception as e:
        logger.error(f"Supabase write error: {e}")

    # Note: caller_name is backfilled via end-of-call-report webhook using booking.call_id

    # Send customer SMS confirmation
    try:
        await send_confirmation(
            to_phone=phone,
            name=name,
            vehicle=vehicle_str or "your vehicle",
            slot=slot_dt or appointment_time,
            shop_name=SHOP_NAME,
            shop_address=SHOP_ADDRESS,
        )
        logger.info(f"SMS confirmation sent to {phone}")
    except Exception as e:
        logger.error(f"SMS confirmation failed: {e}")

    # Send owner alert
    try:
        await send_owner_alert({
            "name": name,
            "phone": phone,
            "vehicle": vehicle_str,
            "issue": issue,
            "slot": appointment_time,
        })
        logger.info("Owner alert sent")
    except Exception as e:
        logger.error(f"Owner alert failed: {e}")

    # Send booking email to shop owner (always, regardless of calendar status)
    try:
        shop_email = shop_row.get("email")
        shop_name_local = shop_row.get("name") or SHOP_NAME
        if shop_email:
            await send_booking_email(
                to_email=shop_email,
                shop_name=shop_name_local,
                customer_name=name,
                customer_phone=phone,
                vehicle=vehicle_str or "Vehicle TBD",
                service=issue,
                appointment_time=slot_dt.strftime("%A, %B %d at %I:%M %p") if slot_dt else appointment_time,
                calendar_link=cal_link or "",
            )
    except Exception as e:
        logger.error(f"Booking email failed: {e}")

    slot_display = slot_dt.strftime("%A, %B %d at %I:%M %p") if slot_dt else appointment_time
    confirmation_msg = (
        f"Booked. {name} confirmed for {issue} on {slot_display}. "
        f"Confirmation text sent to {phone}."
    )

    # Return correct format based on caller
    if is_vapi:
        return JSONResponse({
            "results": [{
                "toolCallId": tool_call_id,
                "result": confirmation_msg
            }]
        })
    else:
        return JSONResponse({
            "confirmation": (
                f"Perfect, you're all set. We've booked {name} for {issue} "
                f"on {slot_display}. A confirmation text is on its way to {phone}. "
                f"We look forward to seeing you!"
            ),
            "success": True,
            "calendar_link": cal_link or "",
        })


@app.post("/vapi/book")
async def vapi_book_appointment(payload: dict):
    """
    Called by Vapi when the AI tool 'bookAppointment' is triggered.
    Vapi sends: {"message": {"toolCalls": [{"function": {"arguments": {...}}}]}}
    """
    try:
        # Extract arguments from Vapi tool call structure
        tool_calls = payload.get("message", {}).get("toolCalls", [])
        if not tool_calls:
            # Direct call with flat args (fallback)
            args = payload
        else:
            args = tool_calls[0].get("function", {}).get("arguments", {})
            if isinstance(args, str):
                import json as _json
                args = _json.loads(args)

        req = VapiBookingRequest(
            customer_name=args.get("customer_name", ""),
            customer_phone=args.get("customer_phone", ""),
            vehicle_year=args.get("vehicle_year", ""),
            vehicle_make=args.get("vehicle_make", ""),
            vehicle_model=args.get("vehicle_model", ""),
            service=args.get("service", ""),
            appointment_time=args.get("appointment_time", "")
        )
    except Exception as e:
        logger.error(f"Failed to parse Vapi booking request: {e} — payload: {payload}")
        return JSONResponse({"result": "I had trouble processing that booking. Let me take your number and have someone call you right back."})

    vehicle_str = f"{req.vehicle_year} {req.vehicle_make} {req.vehicle_model}".strip()
    slot_dt = _parse_appointment_time(req.appointment_time)

    # Book calendar event
    cal_link = None
    try:
        cal = GoogleCalendarService()
        cal_link = await cal.book_appointment(
            name=req.customer_name,
            phone=req.customer_phone,
            vehicle=vehicle_str or "Vehicle TBD",
            issue=req.service,
            slot_datetime=slot_dt,
        )
        logger.info(f"Vapi booking — calendar event created: {cal_link}")
    except Exception as e:
        logger.error(f"Vapi booking — calendar failed: {e}")

    # Send SMS confirmation
    try:
        await send_confirmation(
            to_phone=req.customer_phone,
            name=req.customer_name,
            vehicle=vehicle_str or "your vehicle",
            slot=slot_dt or req.appointment_time,
            shop_name=SHOP_NAME,
            shop_address=SHOP_ADDRESS,
        )
    except Exception as e:
        logger.error(f"Vapi booking — SMS failed: {e}")

    # Owner alert
    try:
        await send_owner_alert({
            "name": req.customer_name,
            "phone": req.customer_phone,
            "vehicle": vehicle_str,
            "issue": req.service,
            "slot": req.appointment_time,
        })
    except Exception as e:
        logger.error(f"Vapi booking — owner alert failed: {e}")

    slot_display = slot_dt.strftime("%A, %B %d at %I:%M %p") if slot_dt else req.appointment_time
    result_msg = (
        f"Booked. {req.customer_name} is confirmed for {req.service} on {slot_display}. "
        f"Confirmation text sent to {req.customer_phone}."
    )

    # Vapi expects {"results": [{"toolCallId": ..., "result": ...}]}
    tool_call_id = tool_calls[0].get("id", "") if tool_calls else ""
    return JSONResponse({
        "results": [{
            "toolCallId": tool_call_id,
            "result": result_msg
        }]
    })


@app.api_route("/slots", methods=["GET", "POST"])
async def get_slots(request=None):
    """
    Called by ElevenLabs to get available appointment slots.
    Returns the next 6 available slots as human-readable strings.
    """
    try:
        cal = GoogleCalendarService()
        slots = await cal.get_available_slots(num_days=3)
        slot_strings = [
            dt.strftime("%A, %B %d at %I:%M %p")
            for dt in slots[:6]
        ]
        return JSONResponse({"slots": slot_strings})
    except Exception as e:
        logger.error(f"Failed to fetch slots: {e}")
        return JSONResponse({"slots": [], "error": str(e)})


@app.get("/health")
async def health():
    return {"status": "ok", "shop": SHOP_NAME}


def _parse_appointment_time(time_str: str) -> Optional[datetime]:
    """Parse a free-form appointment time string into a datetime (Eastern time)."""
    import re
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")

    # Normalize: collapse spaces, handle am/pm variations
    s = time_str.strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'(\d)(am|pm)', r'\1 \2', s, flags=re.IGNORECASE)  # "10am" -> "10 am"

    now = datetime.now(tz=EASTERN)
    current_year = now.year

    # Handle day-of-week only formats like "Thursday at 10am" or "Thursday at noon"
    # Convert "noon" -> "12:00 PM", "midnight" -> "12:00 AM"
    s = re.sub(r'\bnoon\b', '12:00 PM', s, flags=re.IGNORECASE)
    s = re.sub(r'\bmidnight\b', '12:00 AM', s, flags=re.IGNORECASE)

    # If string starts with a day name only (no month), resolve to next occurrence of that day
    day_only_match = re.match(
        r'^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+at\s+(.+)$',
        s, re.IGNORECASE
    )
    if day_only_match:
        day_name = day_only_match.group(1).lower()
        time_part = day_only_match.group(2).strip()
        days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
        target_weekday = days.index(day_name)
        today_weekday = now.weekday()
        days_ahead = (target_weekday - today_weekday) % 7
        if days_ahead == 0:
            days_ahead = 7  # next week if today
        target_date = now + __import__('datetime').timedelta(days=days_ahead)
        # Try to parse the time part
        for time_fmt in ["%I:%M %p", "%I %p", "%H:%M"]:
            try:
                t = datetime.strptime(time_part.strip(), time_fmt)
                return target_date.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            except ValueError:
                continue

    # Strip leading day name before month (e.g. "Thursday April 10 at 10am")
    s_no_day = re.sub(r'^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+', '', s, flags=re.IGNORECASE)

    formats = [
        "%B %d at %I:%M %p",
        "%B %d at %I %p",
        "%B %d, %Y at %I:%M %p",
        "%B %d, %Y at %I %p",
        "%A, %B %d at %I:%M %p",
        "%A, %B %d at %I %p",
        "%A %B %d at %I:%M %p",
        "%A %B %d at %I %p",
        "%B %d %Y at %I:%M %p",
        "%B %d %Y at %I %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    for candidate in [s_no_day, s]:
        for fmt in formats:
            try:
                dt = datetime.strptime(candidate.strip(), fmt)
                if dt.year == 1900:
                    dt = dt.replace(year=current_year)
                return dt
            except ValueError:
                continue

    logger.warning(f"Could not parse appointment time: {time_str!r}")
    return None


if __name__ == "__main__":
    uvicorn.run("booking_webhook:app", host="0.0.0.0", port=8000, reload=True)
