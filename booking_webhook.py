"""
booking_webhook.py — ShopDesk booking tool webhook for Vapi.

Vapi calls POST /book when the AI agent wants to book an appointment.
Returns a confirmation string the AI reads back to the caller.

Run with: uvicorn booking_webhook:app --host 0.0.0.0 --port 8000
Expose with: ngrok http 8000
"""

import os
import logging
import json
import hmac
import hashlib
import base64
from datetime import datetime

import uvicorn
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from typing import Optional, Tuple
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or os.getenv("NEXT_PUBLIC_SUPABASE_URL", "")).strip().strip('"').strip("'")
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")).strip().strip('"').strip("'")
BOOKING_WEBHOOK_SECRET = (os.getenv("BOOKING_WEBHOOK_SECRET", "") or os.getenv("VAPI_WEBHOOK_SECRET", "")).strip()
# ALLOW_UNSAFE_SHOP_QUERY_FALLBACK intentionally removed.
# DEFAULT_SHOP_ID intentionally removed.
# No global shop fallbacks exist in this service.

# Log only key role metadata (never key fragments).
try:
    _payload = SUPABASE_SERVICE_KEY.split('.')[1] if SUPABASE_SERVICE_KEY else ''
    _payload += '=' * (4 - len(_payload) % 4)
    _key_role = json.loads(base64.b64decode(_payload)).get('role', 'unknown') if _payload else 'missing'
except Exception:
    _key_role = 'parse_error'
print(f"[STARTUP] SUPABASE key role: {_key_role}")

# Per-process shop cache: assistant_id -> (shop_row, cached_at_timestamp)
# TTL = 5 min so reprovisions take effect without a restart.
_shop_cache: dict = {}
_SHOP_CACHE_TTL = 300  # seconds

async def get_shop_by_assistant(assistant_id: str) -> dict:
    """Look up shop from Supabase by Vapi assistant ID. Returns shop dict or empty dict."""
    import time
    if not assistant_id:
        return {}
    cached = _shop_cache.get(assistant_id)
    if cached and (time.time() - cached[1]) < _SHOP_CACHE_TTL:
        return cached[0]
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/shops?vapi_agent_id=eq.{assistant_id}&select=id,name,address,email,hours,owner_phone,twilio_number,google_refresh_token,google_calendar_id",
                headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
                timeout=5,
            )
            if resp.status_code == 200 and resp.json():
                import time
                shop = resp.json()[0]
                _shop_cache[assistant_id] = (shop, time.time())
                return shop
    except Exception as e:
        logger.error(f"get_shop_by_assistant error: {e}")
    return {}

async def get_shop_by_id(shop_id: str) -> dict:
    """Look up shop by ID."""
    if not shop_id or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/shops?id=eq.{shop_id}&select=id,name,address,email,hours,owner_phone,twilio_number,google_refresh_token,google_calendar_id",
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


def normalize_phone(phone: str) -> str:
    raw = (phone or "").strip()
    if not raw:
        return ""
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    if raw.startswith('+'):
        return raw
    return digits or raw


async def sync_customer_from_booking(
    shop_id: str,
    customer_name: str,
    customer_phone: str,
    vehicle: str,
    interaction_at: str,
) -> None:
    """Create or enrich a customer record from a successful booking."""
    normalized_phone = normalize_phone(customer_phone)
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not shop_id or not normalized_phone:
        return

    try:
        async with httpx.AsyncClient() as client:
            lookup = await client.get(
                f"{SUPABASE_URL}/rest/v1/customers?shop_id=eq.{shop_id}&phone=eq.{normalized_phone}&select=id,name,vehicle,last_visit,visit_count",
                headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
                timeout=10,
            )
            rows = lookup.json() if lookup.status_code == 200 else []

            if rows:
                existing = rows[0]
                last_visit = existing.get("last_visit")
                next_last_visit = interaction_at
                if last_visit and interaction_at:
                    next_last_visit = max(last_visit, interaction_at)

                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/customers?id=eq.{existing['id']}",
                    json={
                        "name": customer_name or existing.get("name") or "Unknown Customer",
                        "phone": normalized_phone,
                        "vehicle": vehicle or existing.get("vehicle"),
                        "last_visit": next_last_visit,
                        "visit_count": max((existing.get("visit_count") or 0) + 1, 1),
                    },
                    headers={
                        "apikey": SUPABASE_SERVICE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                        "Content-Type": "application/json",
                    },
                    timeout=10,
                )
                return

            await client.post(
                f"{SUPABASE_URL}/rest/v1/customers",
                json={
                    "shop_id": shop_id,
                    "name": customer_name or "Unknown Customer",
                    "phone": normalized_phone,
                    "vehicle": vehicle or None,
                    "last_visit": interaction_at,
                    "visit_count": 1,
                },
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
    except Exception as e:
        logger.error(f"sync_customer_from_booking error: {e}")

async def send_booking_email(to_email: str, shop_name: str, customer_name: str,
                              customer_phone: str, vehicle: str, service: str,
                              appointment_time: str, calendar_link: str = "") -> None:
    """Send booking notification email to shop owner via Resend API."""
    if not to_email or not RESEND_API_KEY:
        logger.warning("Email not configured — skipping booking email")
        return
    try:
        calendar_hint_html = ""
        if not calendar_link:
            calendar_hint_html = "<p style='margin-top:16px;font-size:13px;color:#6b7280'>💡 <a href='https://app.shopdesk.site/dashboard/settings' style='color:#0F7695'>Connect Google Calendar</a> to automatically add bookings.</p>"

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
  {calendar_hint_html}
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


class WebhookAuthError(Exception):
    pass


@app.exception_handler(WebhookAuthError)
async def webhook_auth_exception_handler(_request: Request, exc: WebhookAuthError):
    return JSONResponse({"error": str(exc)}, status_code=401)

# No global shop name/address/phone fallbacks.
# All shop data must be resolved per-request from Supabase.
# Fallbacks to hardcoded values risk cross-shop data leakage.


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

def _safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def _verify_webhook_auth(request: Request, raw_body: bytes) -> bool:
    if not BOOKING_WEBHOOK_SECRET:
        return os.getenv("NODE_ENV", "development") != "production"

    secret = BOOKING_WEBHOOK_SECRET

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and _safe_equal(auth_header[7:].strip(), secret):
        return True

    token_header = request.headers.get("x-booking-webhook-token", "")
    if token_header and _safe_equal(token_header.strip(), secret):
        return True

    sig_header = (
        request.headers.get("x-vapi-signature")
        or request.headers.get("x-vapi-signature-256")
        or request.headers.get("vapi-signature")
        or request.headers.get("x-signature")
        or ""
    )
    if not sig_header:
        return False

    expected_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected_b64 = base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode("utf-8")
    expected = {expected_hex, f"sha256={expected_hex}", expected_b64, f"sha256={expected_b64}"}

    provided = {part.strip() for part in sig_header.split(',') if part.strip()}
    return any(any(_safe_equal(p, e) for e in expected) for p in provided)


def _error_response(is_vapi: bool, tool_call_id: str, message: str, status_code: int = 400):
    if is_vapi:
        return JSONResponse({
            "results": [{
                "toolCallId": tool_call_id,
                "result": message,
            }]
        }, status_code=status_code)
    return JSONResponse({"error": message}, status_code=status_code)


async def _resolve_shop(assistant_id: str, query_shop_id: str) -> Tuple[dict, str]:
    # Primary: use shop_id from URL — reliable, no hallucination risk
    if query_shop_id:
        shop_row = await get_shop_by_id(query_shop_id)
        if shop_row:
            return shop_row, ""
        return {}, f"Shop not found for ID: {query_shop_id}"

    # Fallback: assistant_id from AI (less reliable — AI can hallucinate this)
    if assistant_id:
        shop_row = await get_shop_by_assistant(assistant_id)
        if shop_row:
            return shop_row, ""
        return {}, "Could not resolve shop from assistant ID."

    # No DEFAULT_SHOP_ID fallback in production.
    # Every request must carry a valid shop_id in the URL.
    return {}, "Missing or unresolvable shop context."


def _extract_payload(body: dict) -> Tuple[bool, str, dict, str, str, str, str, str, str, str]:
    tool_calls = body.get("message", {}).get("toolCalls", [])
    is_vapi = bool(tool_calls)

    if is_vapi:
        tc = tool_calls[0]
        tool_call_id = tc.get("id", "")
        raw_args = tc.get("function", {}).get("arguments", "{}")
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

        vehicle_str = f"{args.get('vehicle_year','')} {args.get('vehicle_make','')} {args.get('vehicle_model','')}".strip()
        return (
            True,
            tool_call_id,
            args,
            args.get("customer_name", ""),
            normalize_phone(args.get("customer_phone", "")),
            vehicle_str,
            args.get("service", ""),
            args.get("appointment_time", ""),
            args.get("assistant_id", ""),
            body.get("message", {}).get("call", {}).get("id", ""),
        )

    return (
        False,
        "",
        body,
        body.get("name", ""),
        normalize_phone(body.get("phone", "")),
        body.get("vehicle", ""),
        body.get("issue", ""),
        body.get("appointment_time", ""),
        body.get("assistant_id", ""),
        body.get("call_id", ""),
    )


async def _handle_booking_request(body: dict, query_shop_id: str = ""):
    (
        is_vapi,
        tool_call_id,
        _args,
        name,
        phone,
        vehicle_str,
        issue,
        appointment_time,
        assistant_id,
        vapi_call_id,
    ) = _extract_payload(body)

    if not name or not phone or not issue or not appointment_time:
        return _error_response(is_vapi, tool_call_id, "Missing required booking fields.")

    slot_dt = _parse_appointment_time(appointment_time)

    shop_row, shop_error = await _resolve_shop(assistant_id, query_shop_id)
    if not shop_row:
        return _error_response(is_vapi, tool_call_id, shop_error)

    shop_id_resolved = shop_row.get("id")

    cal_link = None
    try:
        shop_refresh_token = shop_row.get("google_refresh_token")
        shop_calendar_id = shop_row.get("google_calendar_id") or "primary"

        if shop_refresh_token:
            cal = GoogleCalendarService(
                refresh_token=shop_refresh_token,
                client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
                shop_hours=shop_row.get("hours"),
                calendar_id=shop_calendar_id,
            )
            cal_link = await cal.book_appointment(
                name=name,
                phone=phone,
                vehicle=vehicle_str or "Vehicle TBD",
                issue=issue,
                slot_datetime=slot_dt,
            )
            if cal_link and cal_link.startswith("CALENDAR_FAILED:"):
                cal_link = None
    except Exception as e:
        logger.error(f"Calendar booking failed: {e}")

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

    try:
        await send_confirmation(
            to_phone=phone,
            name=name,
            vehicle=vehicle_str or "your vehicle",
            slot=slot_dt or appointment_time,
            shop_name=shop_row.get("name") or "",
            shop_address=shop_row.get("address") or "",
            from_phone=shop_row.get("twilio_number") or "",
        )
    except Exception as e:
        logger.error(f"SMS confirmation failed: {e}")

    try:
        await send_owner_alert({
            "name": name,
            "phone": phone,
            "vehicle": vehicle_str,
            "issue": issue,
            "slot": appointment_time,
            "owner_phone": shop_row.get("owner_phone") or "",
            "shop_name": shop_row.get("name") or "",
            "from_phone": shop_row.get("twilio_number") or "",
        })
    except Exception as e:
        logger.error(f"Owner alert failed: {e}")

    try:
        shop_email = shop_row.get("email")
        if shop_email:
            await send_booking_email(
                to_email=shop_email,
                shop_name=shop_row.get("name") or "",
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

    if is_vapi:
        return JSONResponse({
            "results": [{
                "toolCallId": tool_call_id,
                "result": confirmation_msg,
            }]
        })

    return JSONResponse({
        "confirmation": (
            f"Perfect, you're all set. We've booked {name} for {issue} "
            f"on {slot_display}. A confirmation text is on its way to {phone}. "
            f"We look forward to seeing you!"
        ),
        "success": True,
        "calendar_link": cal_link or "",
    })


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
        normalized_phone = normalize_phone(customer_phone)
        payload = {
            "shop_id": shop_id,
            "customer_name": customer_name,
            "customer_phone": normalized_phone,
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
                await sync_customer_from_booking(
                    shop_id=shop_id,
                    customer_name=customer_name,
                    customer_phone=normalized_phone,
                    vehicle=vehicle or "",
                    interaction_at=appt_iso,
                )
            else:
                logger.error(f"Supabase booking insert failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"save_booking_to_supabase error: {e}")


@app.post("/book")
async def book_appointment(request: Request):
    raw_body = await request.body()
    if not _verify_webhook_auth(request, raw_body):
        raise WebhookAuthError("Unauthorized webhook request")

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

    logger.info(f"Booking request body: {str(body)[:300]}")
    query_shop_id = request.query_params.get("shop_id", "")
    return await _handle_booking_request(body, query_shop_id)


@app.post("/vapi/book")
async def vapi_book_appointment(request: Request):
    raw_body = await request.body()
    if not _verify_webhook_auth(request, raw_body):
        raise WebhookAuthError("Unauthorized webhook request")

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

    query_shop_id = request.query_params.get("shop_id", "")
    return await _handle_booking_request(body, query_shop_id)


# /slots removed — single-tenant legacy endpoint with no shop context.


@app.get("/health")
async def health():
    return {"status": "ok", "service": "shopdesk-booking"}


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
