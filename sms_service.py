"""
sms_service.py — Twilio SMS notifications for ShopDesk AI Receptionist.

Three outbound SMS types:
  - send_confirmation()    → customer booking confirmation
  - send_owner_alert()     → owner notification of new booking
  - send_emergency_alert() → urgent owner alert for emergency callers
"""

import os
import logging
import asyncio
from datetime import datetime
from typing import Optional

from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER", "")
OWNER_PHONE = os.getenv("OWNER_PHONE", os.getenv("SHOP_PHONE", "(407) 555-0123"))
SHOP_NAME = os.getenv("SHOP_NAME", "Mike's Auto Repair")

_twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format (+1XXXXXXXXXX) for Twilio."""
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    return phone  # return as-is if we can't parse it


def _fmt_slot(slot) -> str:
    if not slot:
        return "TBD"
    if isinstance(slot, datetime):
        return slot.strftime("%A, %B %d at %I:%M %p")
    return str(slot)


def _send_sms_sync(to: str, body: str) -> str:
    """Blocking SMS send; returns message SID."""
    to = _normalize_phone(to)
    msg = _twilio.messages.create(body=body, from_=TWILIO_PHONE, to=to)
    logger.info(f"SMS sent to {to}: SID={msg.sid}")
    return msg.sid


async def _send_sms(to: str, body: str) -> str:
    """Async wrapper around the blocking Twilio call."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _send_sms_sync, to, body)
    except Exception as e:
        logger.error(f"SMS to {to} failed: {e}")
        return ""


# ── Public API ────────────────────────────────────────────────────────────────

async def send_confirmation(
    to_phone: str,
    name: str,
    vehicle: str,
    slot,
    shop_name: str,
    shop_address: str,
) -> str:
    """Send booking confirmation to the customer."""
    slot_str = _fmt_slot(slot)
    body = (
        f"Hi {name}! Your appointment at {shop_name} is confirmed.\n"
        f"Date: {slot_str}\n"
        f"Vehicle: {vehicle}\n"
        f"Address: {shop_address}\n"
        f"Questions? Call us at {TWILIO_PHONE}. See you soon!\n"
        f"Reply STOP to opt out."
    )
    return await _send_sms(to_phone, body)


async def send_owner_alert(booking_info: dict) -> str:
    """Notify the shop owner of a new appointment booking."""
    name = booking_info.get("name", "Unknown")
    phone = booking_info.get("phone", "Unknown")
    year = booking_info.get("year", "")
    make = booking_info.get("make", "")
    model = booking_info.get("model", "")
    vehicle = booking_info.get("vehicle") or f"{year} {make} {model}".strip() or "Unknown vehicle"
    issue = booking_info.get("issue", "Service")
    slot_str = _fmt_slot(booking_info.get("slot"))
    owner_phone = booking_info.get("owner_phone") or OWNER_PHONE
    shop_name = booking_info.get("shop_name") or SHOP_NAME

    body = (
        f"NEW APPOINTMENT — {shop_name}\n"
        f"Customer: {name} | {phone}\n"
        f"Vehicle: {vehicle}\n"
        f"Service: {issue}\n"
        f"Time: {slot_str}\n"
        f"— ShopDesk AI\n"
        f"Reply STOP to opt out."
    )
    return await _send_sms(owner_phone, body)


async def send_emergency_alert(caller_info: dict) -> str:
    """Send an urgent emergency alert to the shop owner."""
    caller = caller_info.get("caller", "Unknown")
    message = caller_info.get("message", "")
    call_sid = caller_info.get("call_sid", "")
    owner_phone = caller_info.get("owner_phone") or OWNER_PHONE
    shop_name = caller_info.get("shop_name") or SHOP_NAME

    body = (
        f"EMERGENCY ALERT — {shop_name}\n"
        f"Caller: {caller}\n"
        f'Reported: "{message}"\n'
        f"Call SID: {call_sid}\n"
        f"— ShopDesk AI (call in progress)\n"
        f"Reply STOP to opt out."
    )
    return await _send_sms(owner_phone, body)
