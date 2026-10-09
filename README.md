# ShopDesk Booking Service

The booking backend for **ShopDesk's AI phone receptionist**. During a live call, the Vapi voice agent calls this service as a tool to book an appointment. The service figures out which business the call belongs to, creates the booking, puts it on the business's Google Calendar, saves it to Supabase, and sends confirmations by SMS and email. It returns a short confirmation sentence that the AI reads back to the caller.

The owner-facing web app lives in [`shopdesk-dashboard`](../shopdesk-dashboard).

## Request flow

```
Vapi voice agent ──POST /vapi/book (signed)──► booking service
                                               ├─ verify signature or shared token (fails closed)
                                               ├─ resolve the shop from the Vapi assistant ID (cached 5 min)
                                               ├─ parse the requested time ("Thursday at 10am" → datetime, Eastern)
                                               ├─ Google Calendar: create the event on the shop's calendar
                                               ├─ Supabase: save the booking and upsert the customer
                                               ├─ Twilio SMS: confirmation to the caller, alert to the owner
                                               └─ Resend email: confirmation
                                               ◄── "You're booked for Thursday at 10:00 AM…"
```

## Design choices

- **Multi-tenant by construction.** Every booking is tied to the shop that owns the calling assistant. There are no global fallbacks: if shop-specific data (number, owner phone, calendar) is missing, the action is skipped and logged instead of being sent to the wrong business.
- **Fails closed.** If `BOOKING_WEBHOOK_SECRET` isn't configured, every request is rejected. Requests are accepted only with a valid bearer token, a shared token header, or an HMAC-SHA256 signature of the raw body, all compared in constant time.
- **No PII in logs.** Logs record whether a name or phone was present, not the values.
- **Async I/O.** FastAPI with `httpx`; the synchronous Google client runs in an executor.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/vapi/book` | Vapi tool-call format |
| `POST` | `/book` | Plain JSON format (same logic) |
| `GET` | `/health` | Health check |

Interactive API docs are served at `/docs` when running locally.

## Tech stack

Python 3.9+, FastAPI, Uvicorn, Pydantic, httpx · Google Calendar API (OAuth) · Twilio · Resend · Supabase (REST) · pytest, GitHub Actions · deployed on Render (`render.yaml`).

## Running locally

```bash
git clone <this repo>
cd shopdesk-booking
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # fill in your own keys
uvicorn booking_webhook:app --reload --port 8000
# http://localhost:8000/health  ·  http://localhost:8000/docs
```

## Tests

```bash
python -m pytest -q
```

The tests cover the health endpoint, webhook authentication (missing secret, wrong token, valid token, valid and tampered HMAC signatures), phone-number normalisation, and natural-language time parsing. CI runs them on Python 3.9 and 3.12 for every push and pull request.

## Files

| File | What it does |
|---|---|
| `booking_webhook.py` | FastAPI app: auth, shop resolution, booking flow, Supabase writes, email |
| `calendar_service.py` | Google Calendar availability and event creation (per-shop OAuth tokens) |
| `sms_service.py` | Per-shop Twilio SMS: customer confirmation, owner alert, emergency alert |
| `render.yaml` / `Procfile` | Deployment config |

## Author

Built by **Slarve Benoit** ([@CodeBen98](https://github.com/CodeBen98)).
