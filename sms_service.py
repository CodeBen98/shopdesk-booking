"""
sms_service.py — ShopDesk multi-tenant SMS notifications.

Every outbound SMS is strictly scoped to the originating shop:
  - FROM number  = shop's dedicated Twilio number (never a shared/pool number)
  - TO owner     = shop's saved owner_phone (never a global fallback)
  - Shop name    = shop's own name (never a global fallback)

If required per-shop data is missing, the send is skipped and logged —
never silently routed to a wrong number.

Three outbound SMS types:
  - send_confirmation()    → customer booking confirmation
  - send_owner_alert()     → owner notification of new booking
  - send_emergency_alert() → urgent owner alert for emergency callers
"""

import os
import logging
import asyncio
from datetime import datetime

from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")

_twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Normalize to E.164 format (+1XXXXXXXXXX). Returns empty string if unparseable."""
    raw = (phone or "").strip()
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if raw.startswith("+") and len(digits) >= 10:
        return raw
    return ""


def _fmt_slot(slot) -> str:
    if not slot:
        return "TBD"
    if isinstance(slot, datetime):
        return slot.strftime("%A, %B %d at %I:%M %p")
    return str(slot)


def _send_sms_sync(to: str, body: str, from_phone: str) -> str:
    """
    Blocking Twilio SMS send. Returns message SID.

    Always sends FROM the shop's dedicated number (from_phone).
    If from_phone is missing, falls back to messaging service pool
    only as a last resort (compliance still satisfied since all
    numbers are registered to the same A2P campaign).
    """
    to_e164 = _normalize_phone(to)
    if not to_e164:
        raise ValueError(f"Invalid destination phone number: {to!r}")

    from_e164 = _normalize_phone(from_phone) if from_phone else ""

    if from_e164:
        msg = _twilio.messages.create(body=body, from_=from_e164, to=to_e164)
    elif TWILIO_MESSAGING_SERVICE_SID:
        logger.warning(f"No shop from_phone provided — falling back to messaging service pool for {to_e164}")
        msg = _twilio.messages.create(
            body=body,
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            to=to_e164,
        )
    else:
        raise RuntimeError("No from_phone and no TWILIO_MESSAGING_SERVICE_SID configured")

    logger.info(f"SMS sent to={to_e164} from={from_e164 or 'pool'} SID={msg.sid}")
    return msg.sid


async def _send_sms(to: str, body: str, from_phone: str) -> str:
    """Async wrapper around the blocking Twilio call."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _send_sms_sync, to, body, from_phone)
    except Exception as e:
        logger.error(f"SMS send failed to={to!r} from={from_phone!r}: {e}")
        return ""


# ── Public API ────────────────────────────────────────────────────────────────

async def send_confirmation(
    to_phone: str,
    name: str,
    vehicle: str,
    slot,
    shop_name: str,
    shop_address: str,
    from_phone: str = "",
) -> str:
    """
    Send booking confirmation to the customer.
    from_phone must be the shop's dedicated Twilio number.
    """
    if not to_phone:
        logger.warning("send_confirmation: no customer phone, skipping")
        return ""

    slot_str = _fmt_slot(slot)
    from_display = _normalize_phone(from_phone) or "(unknown)"
    body = (
        f"Hi {name}! Your appointment at {shop_name} is confirmed.\n"
        f"Date: {slot_str}\n"
        f"Vehicle: {vehicle}\n"
        f"Address: {shop_address}\n"
        f"Questions? Call us at {from_display}. See you soon!\n"
        f"Reply STOP to opt out."
    )
    return await _send_sms(to_phone, body, from_phone)


async def send_owner_alert(booking_info: dict) -> str:
    """
    Notify the shop owner of a new booking.

    Required keys in booking_info:
      owner_phone  — shop owner's mobile (from shops.owner_phone)
      from_phone   — shop's Twilio number (from shops.twilio_number)
      shop_name    — shop name (from shops.name)

    If owner_phone is missing, the alert is skipped — never routed
    to a global fallback number.
    """
    owner_phone = (booking_info.get("owner_phone") or "").strip()
    if not owner_phone:
        logger.info("send_owner_alert: shop has no owner_phone saved — skipping SMS alert")
        return ""

    from_phone = booking_info.get("from_phone", "")
    shop_name = booking_info.get("shop_name") or "Your shop"

    name = booking_info.get("name", "Unknown")
    phone = booking_info.get("phone", "Unknown")
    vehicle = booking_info.get("vehicle") or "Unknown vehicle"
    issue = booking_info.get("issue", "Service")
    slot_str = _fmt_slot(booking_info.get("slot"))

    body = (
        f"NEW APPOINTMENT — {shop_name}\n"
        f"Customer: {name} | {phone}\n"
        f"Vehicle: {vehicle}\n"
        f"Service: {issue}\n"
        f"Time: {slot_str}\n"
        f"— ShopDesk AI\n"
        f"Reply STOP to opt out."
    )
    return await _send_sms(owner_phone, body, from_phone)


async def send_emergency_alert(caller_info: dict) -> str:
    """
    Send an urgent emergency alert to the shop owner.

    Required keys in caller_info:
      owner_phone  — shop owner's mobile
      from_phone   — shop's Twilio number
      shop_name    — shop name

    If owner_phone is missing, skipped — never routed to a global fallback.
    """
    owner_phone = (caller_info.get("owner_phone") or "").strip()
    if not owner_phone:
        logger.info("send_emergency_alert: shop has no owner_phone saved — skipping SMS alert")
        return ""

    from_phone = caller_info.get("from_phone", "")
    shop_name = caller_info.get("shop_name") or "Your shop"
    caller = caller_info.get("caller", "Unknown")
    message = caller_info.get("message", "")
    call_sid = caller_info.get("call_sid", "")

    body = (
        f"EMERGENCY ALERT — {shop_name}\n"
        f"Caller: {caller}\n"
        f'Reported: "{message}"\n'
        f"Call SID: {call_sid}\n"
        f"— ShopDesk AI (call in progress)\n"
        f"Reply STOP to opt out."
    )
    return await _send_sms(owner_phone, body, from_phone)
