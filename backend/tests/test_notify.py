"""Tests for booking confirmations (Phase 3). Twilio is faked: no real messages are sent.
Run from the backend folder:  python -m pytest tests -q
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BUSINESS_TZ", "Asia/Kolkata")
os.environ.pop("DATABASE_URL", None)  # tests always use a throwaway SQLite file

import db  # noqa: E402

db.DB_PATH = Path(__file__).parent / "test_salon.db"

from fastapi.testclient import TestClient  # noqa: E402
from twilio.base.exceptions import TwilioRestException  # noqa: E402

import main  # noqa: E402
import notify  # noqa: E402

TWILIO_ENV = {
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "token",
    "TWILIO_FROM": "whatsapp:+14155238886",
}


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "")  # no API key needed in these tests
    db.DB_PATH.unlink(missing_ok=True)
    db.init()
    yield
    db.DB_PATH.unlink(missing_ok=True)


def next_open_day() -> str:
    d = date.today() + timedelta(days=1)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


def book(client: TestClient):
    return client.post("/appointments", json={
        "service_id": 1, "customer_name": "Arya", "phone": "98765 43210",
        "start_time": f"{next_open_day()}T15:00",
    })


def test_booking_sends_whatsapp_confirmation(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    with patch.dict(os.environ, TWILIO_ENV), patch("twilio.rest.Client") as FakeClient:
        FakeClient.return_value.messages.create.return_value = MagicMock(sid="SM123")
        r = book(TestClient(main.app))
    assert r.status_code == 201
    assert r.json()["confirmation_sent"] is True
    kwargs = FakeClient.return_value.messages.create.call_args.kwargs
    assert kwargs["to"] == "whatsapp:+919876543210"
    assert kwargs["from_"] == "whatsapp:+14155238886"
    assert "Haircut" in kwargs["body"] and "Arya" in kwargs["body"] and "3:00 PM" in kwargs["body"]


def test_reschedule_and_cancel_also_notify(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    with patch.dict(os.environ, TWILIO_ENV), patch("twilio.rest.Client") as FakeClient:
        FakeClient.return_value.messages.create.return_value = MagicMock(sid="SM1")
        c = TestClient(main.app)
        appt_id = book(c).json()["id"]
        c.patch(f"/appointments/{appt_id}", json={"start_time": f"{next_open_day()}T16:00"})
        c.post(f"/appointments/{appt_id}/cancel")
    bodies = [call.kwargs["body"] for call in FakeClient.return_value.messages.create.call_args_list]
    assert len(bodies) == 3
    assert "confirmed" in bodies[0] and "moved" in bodies[1] and "cancelled" in bodies[2]


def test_channel_none_sends_nothing(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "none")
    with patch("twilio.rest.Client") as FakeClient:
        r = book(TestClient(main.app))
    assert r.json()["confirmation_sent"] is False
    FakeClient.assert_not_called()


def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    with patch.dict(os.environ, {}, clear=False):
        for k in TWILIO_ENV:
            os.environ.pop(k, None)
        with pytest.raises(RuntimeError, match="TWILIO_ACCOUNT_SID"):
            notify.send_now("9876543210", "hi")


def test_number_not_in_sandbox_gets_a_clear_hint(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    err = TwilioRestException(status=400, uri="/Messages", msg="sandbox", code=63015)
    with patch.dict(os.environ, TWILIO_ENV), patch("twilio.rest.Client") as FakeClient:
        FakeClient.return_value.messages.create.side_effect = err
        with pytest.raises(RuntimeError, match="hasn't joined your WhatsApp sandbox"):
            notify.send_now("9876543210", "hi")


def test_failed_send_never_breaks_the_booking(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    with patch.dict(os.environ, TWILIO_ENV), patch("twilio.rest.Client") as FakeClient:
        FakeClient.return_value.messages.create.side_effect = RuntimeError("Twilio is down")
        r = book(TestClient(main.app))
    assert r.status_code == 201  # the booking is saved even though the message failed


# ---------- template mode (new Twilio accounts require WhatsApp templates) ----------

def test_template_mode_sends_content_sid_with_date_and_time(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "CONTENT_SID", "HXtemplate123")
    with patch.dict(os.environ, TWILIO_ENV), patch("twilio.rest.Client") as FakeClient:
        FakeClient.return_value.messages.create.return_value = MagicMock(sid="SM9")
        r = book(TestClient(main.app))
    assert r.json()["confirmation_sent"] is True
    kwargs = FakeClient.return_value.messages.create.call_args.kwargs
    assert kwargs["content_sid"] == "HXtemplate123"
    assert "body" not in kwargs  # templates replace free-form text
    variables = json.loads(kwargs["content_variables"])
    assert variables["2"] == "3:00 PM"
    assert "," in variables["1"]  # e.g. "Tuesday, 6 October"


def test_template_mode_skips_cancellations_and_says_so(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "CONTENT_SID", "HXtemplate123")
    with patch.dict(os.environ, TWILIO_ENV), patch("twilio.rest.Client") as FakeClient:
        FakeClient.return_value.messages.create.return_value = MagicMock(sid="SM9")
        c = TestClient(main.app)
        appt_id = book(c).json()["id"]
        cancel = c.post(f"/appointments/{appt_id}/cancel").json()
    # only the booking was sent; the cancellation wasn't (and the API doesn't claim it was)
    assert FakeClient.return_value.messages.create.call_count == 1
    assert cancel["confirmation_sent"] is False


def test_contentsid_error_explains_the_fix(monkeypatch):
    monkeypatch.setattr(notify, "CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "CONTENT_SID", "")
    err = TwilioRestException(status=400, uri="/Messages", msg="ContentSid required", code=21654)
    with patch.dict(os.environ, TWILIO_ENV), patch("twilio.rest.Client") as FakeClient:
        FakeClient.return_value.messages.create.side_effect = err
        with pytest.raises(RuntimeError, match="TWILIO_CONTENT_SID"):
            notify.send_now("9876543210", "hi")