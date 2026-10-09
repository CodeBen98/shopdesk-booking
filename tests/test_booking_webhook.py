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
