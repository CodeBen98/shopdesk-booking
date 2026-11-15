import hashlib
import hmac
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import booking_webhook as bw

client = TestClient(bw.app)
SECRET = "test-secret"
BODY = json.dumps({"message": {"type": "tool-calls"}}).encode()


@pytest.fixture
def with_secret(monkeypatch):
    monkeypatch.setattr(bw, "BOOKING_WEBHOOK_SECRET", SECRET)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_rejects_everything_when_secret_not_configured(monkeypatch):
    monkeypatch.setattr(bw, "BOOKING_WEBHOOK_SECRET", "")
    r = client.post("/book", content=BODY, headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 401


def test_rejects_wrong_bearer_token(with_secret):
    r = client.post("/book", content=BODY, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_rejects_missing_credentials(with_secret):
    assert client.post("/book", content=BODY).status_code == 401


def test_accepts_correct_bearer_token(with_secret):
    r = client.post("/book", content=BODY, headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code != 401          # passes auth; fails later only because the payload has no booking


def test_accepts_valid_hmac_signature(with_secret):
    sig = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    r = client.post("/book", content=BODY, headers={"x-vapi-signature": f"sha256={sig}"})
    assert r.status_code != 401


def test_rejects_tampered_body(with_secret):
    sig = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    r = client.post("/book", content=BODY + b" ", headers={"x-vapi-signature": sig})
    assert r.status_code == 401


@pytest.mark.parametrize("raw,expected", [
    ("(407) 555-1234", "+14075551234"),
    ("407.555.1234", "+14075551234"),
    ("1 407 555 1234", "+14075551234"),
    ("+447946095800", "+447946095800"),
    ("", ""),
])
def test_normalize_phone(raw, expected):
    assert bw.normalize_phone(raw) == expected


def test_parses_relative_day_and_time():
    dt = bw._parse_appointment_time("Thursday at 10am")
    assert dt is not None
    assert dt.weekday() == 3 and dt.hour == 10 and dt.minute == 0
    assert dt > datetime.now(tz=ZoneInfo("America/New_York"))


def test_health_allows_get_and_head():
    assert client.get("/health").status_code == 200
    assert client.head("/health").status_code == 200


@pytest.mark.parametrize("db,expected_code", [
    ("ok", 200),
    ("not_configured", 200),
    ("unreachable", 503),
    ("http_500", 503),
])
def test_health_reports_database(monkeypatch, db, expected_code):
    async def fake_check():
        return db
    monkeypatch.setattr(bw, "_check_database", fake_check)
    r = client.get("/health")
    assert r.status_code == expected_code
    assert r.json()["database"] == db


def test_parses_in_shop_timezone():
    la = bw._parse_appointment_time("March 3, 2031 at 10am", "America/Los_Angeles")
    assert la.tzinfo is not None
    assert (la.hour, la.utcoffset().total_seconds()) == (10, -8 * 3600)
    ny = bw._parse_appointment_time("March 3, 2031 at 10am")
    assert ny.utcoffset().total_seconds() == -5 * 3600
    # Same wall-clock time, 3 hours apart in real time.
    assert (la - ny).total_seconds() == 3 * 3600


def test_relative_day_uses_shop_timezone():
    dt = bw._parse_appointment_time("Friday at 2:30 pm", "America/Los_Angeles")
    assert str(dt.tzinfo) == "America/Los_Angeles"
    assert (dt.weekday(), dt.hour, dt.minute) == (4, 14, 30)


def test_month_day_without_year_rolls_into_next_year(monkeypatch):
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 20, 9, 0, tzinfo=tz)
    monkeypatch.setattr(bw, "datetime", FakeDatetime)
    dt = bw._parse_appointment_time("January 5 at 9am", "America/Los_Angeles")
    assert (dt.year, dt.month, dt.day) == (2027, 1, 5)


@pytest.mark.parametrize("tz,expected", [
    ("America/Los_Angeles", "America/Los_Angeles"),
    (None, "America/New_York"),
    ("Mars/Olympus", "America/New_York"),
])
def test_shop_timezone_fallback(tz, expected):
    assert bw.shop_timezone({"timezone": tz}) == expected


def test_booking_is_saved_in_shop_timezone(monkeypatch):
    import asyncio
    saved = {}

    async def fake_resolve(assistant_id, shop_id):
        return {"id": "shop-1", "name": "LA Auto", "timezone": "America/Los_Angeles"}, ""

    async def fake_save(**kwargs):
        saved.update(kwargs)

    async def noop(*a, **k):
        return ""

    monkeypatch.setattr(bw, "_resolve_shop", fake_resolve)
    monkeypatch.setattr(bw, "save_booking_to_supabase", fake_save)
    monkeypatch.setattr(bw, "send_confirmation", noop)
    monkeypatch.setattr(bw, "send_owner_alert", noop)

    body = {"name": "Ana", "phone": "3105551234", "issue": "Oil change",
            "appointment_time": "March 3, 2031 at 10am"}
    resp = asyncio.run(bw._handle_booking_request(body, "shop-1"))
    assert resp.status_code == 200
    assert saved["timezone"] == "America/Los_Angeles"
    assert saved["slot_dt"].isoformat() == "2031-03-03T10:00:00-08:00"
