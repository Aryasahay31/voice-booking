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


# Twilio error codes people commonly hit, explained in plain words.
_TWILIO_HINTS = {
    63015: "This number hasn't joined your WhatsApp sandbox. From that phone, send the "
           "'join <your-code>' message to the sandbox number shown in the Twilio console.",
    63016: "WhatsApp only allows free-form messages within 24 hours of the customer's last "
           "message to you. In the sandbox, send the 'join' message again.",
    21211: "The phone number isn't valid. Check DEFAULT_COUNTRY_CODE and the number itself.",
    20003: "Twilio rejected the login. Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
    21606: "TWILIO_FROM isn't a number your account can send from. For the sandbox, use the "
           "sandbox number from the Twilio console, e.g. whatsapp:+14155238886.",
}


def send_now(phone_digits: str, body: str) -> str:
    """Send one message immediately. Returns Twilio's message id, or raises with a
    plain-language explanation. Used by send() and by send_test_message.py."""
    if not enabled():
        raise RuntimeError("NOTIFY_CHANNEL is 'none', so nothing is sent. Set it to 'whatsapp'.")
    missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing settings: {', '.join(missing)}")

    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    to, from_ = to_e164(phone_digits), os.environ["TWILIO_FROM"].strip()
    if CHANNEL == "whatsapp":
        to = f"whatsapp:{to}"
        if not from_.startswith("whatsapp:"):
            from_ = f"whatsapp:{from_}"
    try:
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        return client.messages.create(to=to, from_=from_, body=body).sid
    except TwilioRestException as e:
        hint = _TWILIO_HINTS.get(e.code, "See the Twilio console's error logs for details.")
        raise RuntimeError(f"Twilio error {e.code}: {hint}") from e


def send(phone_digits: str, body: str) -> None:
    """Runs in a FastAPI background task, so a slow or failed send never blocks a booking."""
    if not enabled():
        logger.info("[confirmation not sent: NOTIFY_CHANNEL=none] to %s: %s", phone_digits, body)
        return
    try:
        sid = send_now(phone_digits, body)
        logger.info("confirmation sent via %s to %s (sid %s)", CHANNEL, to_e164(phone_digits), sid)
    except Exception as e:
        logger.error("confirmation NOT sent to %s: %s", phone_digits, e)


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