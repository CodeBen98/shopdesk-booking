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
DEFAULT_SHOP_ID = os.getenv("DEFAULT_SHOP_ID", "")

from calendar_service import GoogleCalendarService
from sms_service import send_confirmation, send_owner_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="ShopDesk Booking Webhook")

SHOP_NAME = os.getenv("SHOP_NAME", "Mike's Auto Repair")
SHOP_ADDRESS = os.getenv("SHOP_ADDRESS", "1234 Orange Ave, Orlando, FL 32801")
SHOP_PHONE = os.getenv("SHOP_PHONE", "(407) 555-0123")


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
    calendar_link: str = ""
) -> None:
    """Write booking to Supabase bookings table. Never raises — failures are logged only."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not shop_id:
        logger.warning("Supabase env vars not set — skipping DB write")
        return
    try:
        appt_iso = slot_dt.isoformat() if slot_dt else appointment_time
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
    else:
        # Legacy ElevenLabs format
        name = body.get("name", "")
        phone = body.get("phone", "")
        vehicle_str = body.get("vehicle", "")
        issue = body.get("issue", "")
        appointment_time = body.get("appointment_time", "")
        tool_call_id = None

    slot_dt = _parse_appointment_time(appointment_time)

    # Book calendar event — use per-shop refresh token if available
    cal_link = None
    try:
        # Try to get per-shop Google refresh token from Supabase
        shop_refresh_token = None
        shop_calendar_id = CALENDAR_ID
        if SUPABASE_URL and SUPABASE_SERVICE_KEY and DEFAULT_SHOP_ID:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/shops?id=eq.{DEFAULT_SHOP_ID}&select=google_refresh_token,google_calendar_id",
                    headers={
                        "apikey": SUPABASE_SERVICE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    },
                    timeout=5,
                )
                if resp.status_code == 200:
                    rows = resp.json()
                    if rows:
                        shop_refresh_token = rows[0].get("google_refresh_token")
                        shop_calendar_id = rows[0].get("google_calendar_id") or CALENDAR_ID

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
            )
            cal_link = await cal.book_appointment(
                name=name,
                phone=phone,
                vehicle=vehicle_str or "Vehicle TBD",
                issue=issue,
                slot_datetime=slot_dt,
            )
            logger.info(f"Calendar event created: {cal_link}")
    except Exception as e:
        logger.error(f"Calendar booking failed: {e}")

    # Write booking to Supabase
    try:
        await save_booking_to_supabase(
            shop_id=DEFAULT_SHOP_ID,
            customer_name=name,
            customer_phone=phone,
            vehicle=vehicle_str or "",
            service=issue,
            slot_dt=slot_dt,
            appointment_time=appointment_time,
            calendar_link=cal_link or "",
        )
    except Exception as e:
        logger.error(f"Supabase write error: {e}")

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
    """Parse a free-form appointment time string into a datetime."""
    import re

    # Normalize: lowercase, collapse spaces, handle am/pm variations
    s = time_str.strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'(\d)(am|pm)', r'\1 \2', s, flags=re.IGNORECASE)  # "10am" -> "10 am"
    s = re.sub(r'^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+', '', s, flags=re.IGNORECASE)  # strip day name

    formats = [
        "%B %d at %I:%M %p",
        "%B %d at %I %p",
        "%B %d, %Y at %I:%M %p",
        "%B %d, %Y at %I %p",
        "%A, %B %d at %I:%M %p",
        "%A, %B %d at %I %p",
        "%A %B %d at %I:%M %p",
        "%A %B %d at %I %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue

    logger.warning(f"Could not parse appointment time: {time_str!r}")
    return None


if __name__ == "__main__":
    uvicorn.run("booking_webhook:app", host="0.0.0.0", port=8000, reload=True)
