"""Booking confirmations over WhatsApp or SMS (via Twilio).

Set NOTIFY_CHANNEL in backend/.env:
  none      -> just print the message in the terminal (default, good for testing)
  whatsapp  -> Twilio WhatsApp (sandbox is free and easiest to start with)
  sms       -> Twilio SMS
"""
import logging
import os
from datetime import datetime

# Reuse uvicorn's logger so messages show up in the backend terminal.
logger = logging.getLogger("uvicorn.error")

CHANNEL = os.getenv("NOTIFY_CHANNEL", "none").lower()
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "91")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Glow Studio")
STAFF_PHONE = os.getenv("STAFF_PHONE", "")  # optional: salon owner's number for callback alerts


def enabled() -> bool:
    return CHANNEL in ("whatsapp", "sms")


def to_e164(phone_digits: str) -> str:
    """'9876543210' -> '+919876543210'; numbers that already include a country code are kept."""
    if len(phone_digits) == 10:
        return f"+{DEFAULT_COUNTRY_CODE}{phone_digits}"
    return f"+{phone_digits}"


def _when(start_iso: str) -> str:
    dt = datetime.fromisoformat(start_iso)
    return f"{dt:%A}, {dt.day} {dt:%B} at {dt.hour % 12 or 12}:{dt:%M} {dt:%p}"


def send(phone_digits: str, body: str) -> None:
    """Runs in a FastAPI background task, so a slow or failed send never blocks a booking."""
    if not enabled():
        logger.info("[confirmation not sent: NOTIFY_CHANNEL=none] to %s: %s", phone_digits, body)
        return
    try:
        from twilio.rest import Client

        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        to, from_ = to_e164(phone_digits), os.environ["TWILIO_FROM"]
        if CHANNEL == "whatsapp":
            to = f"whatsapp:{to}"
            if not from_.startswith("whatsapp:"):
                from_ = f"whatsapp:{from_}"
        msg = client.messages.create(to=to, from_=from_, body=body)
        logger.info("confirmation sent via %s to %s (sid %s)", CHANNEL, to, msg.sid)
    except Exception:
        logger.exception("failed to send confirmation to %s", phone_digits)


def booking_confirmed(appt: dict) -> None:
    send(
        appt["phone"],
        f"Hi {appt['customer_name']}, your {appt['service']} at {BUSINESS_NAME} is confirmed "
        f"for {_when(appt['start_time'])}. Booking ID: {appt['id']}. See you soon!",
    )


def booking_rescheduled(appt: dict) -> None:
    send(
        appt["phone"],
        f"Hi {appt['customer_name']}, your {appt['service']} at {BUSINESS_NAME} has been moved "
        f"to {_when(appt['start_time'])}. Booking ID: {appt['id']}.",
    )


def booking_cancelled(appt: dict) -> None:
    send(
        appt["phone"],
        f"Hi {appt['customer_name']}, your {appt['service']} at {BUSINESS_NAME} on "
        f"{_when(appt['start_time'])} has been cancelled. We hope to see you another time!",
    )


def staff_callback_alert(cb: dict) -> None:
    """Always shows in the backend terminal; also messages staff if STAFF_PHONE is set."""
    tag = "URGENT " if cb["urgency"] == "urgent" else ""
    text = (f"{tag}Callback request #{cb['id']}: {cb['customer_name']} "
            f"({to_e164(cb['phone'])}) - {cb['reason']}")
    logger.warning("NEW %s", text)
    if STAFF_PHONE:
        send("".join(c for c in STAFF_PHONE if c.isdigit()), text)