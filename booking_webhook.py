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
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from typing import Optional
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

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


@app.post("/book")
async def book_appointment(req: BookingRequest):
    """
    Called by ElevenLabs when the AI has collected all booking info.
    Creates a Google Calendar event + sends SMS confirmation + owner alert.
    Returns a short confirmation string the AI reads back to the caller.
    """
    logger.info(f"Booking request: {req}")

    # Try to parse the appointment time
    slot_dt = _parse_appointment_time(req.appointment_time)

    # Book calendar event
    cal_link = None
    try:
        cal = GoogleCalendarService()
        cal_link = await cal.book_appointment(
            name=req.name,
            phone=req.phone,
            vehicle=req.vehicle,
            issue=req.issue,
            slot_datetime=slot_dt,
        )
        logger.info(f"Calendar event created: {cal_link}")
    except Exception as e:
        logger.error(f"Calendar booking failed: {e}")

    # Send customer SMS confirmation
    try:
        await send_confirmation(
            to_phone=req.phone,
            name=req.name,
            vehicle=req.vehicle,
            slot=slot_dt or req.appointment_time,
            shop_name=SHOP_NAME,
            shop_address=SHOP_ADDRESS,
        )
        logger.info(f"SMS confirmation sent to {req.phone}")
    except Exception as e:
        logger.error(f"SMS confirmation failed: {e}")

    # Send owner alert
    try:
        await send_owner_alert({
            "name": req.name,
            "phone": req.phone,
            "vehicle": req.vehicle,
            "issue": req.issue,
            "slot": req.appointment_time,
        })
        logger.info("Owner alert sent")
    except Exception as e:
        logger.error(f"Owner alert failed: {e}")

    # Return confirmation message the AI will read back
    slot_display = slot_dt.strftime("%A, %B %d at %I:%M %p") if slot_dt else req.appointment_time
    return JSONResponse({
        "confirmation": (
            f"Perfect, you're all set. We've booked {req.name} for {req.issue} "
            f"on {slot_display}. A confirmation text is on its way to {req.phone}. "
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


@app.get("/slots")
async def get_slots():
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
    """Try to parse a free-form appointment time string into a datetime."""
    formats = [
        "%A, %B %d at %I:%M %p",
        "%A %B %d at %I:%M %p",
        "%A, %B %d at %I %p",
        "%B %d at %I:%M %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(time_str.strip(), fmt)
            # If no year, assume current year
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    logger.warning(f"Could not parse appointment time: {time_str!r}")
    return None


if __name__ == "__main__":
    uvicorn.run("booking_webhook:app", host="0.0.0.0", port=8000, reload=True)
