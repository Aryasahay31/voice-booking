"""Send one test WhatsApp/SMS message, to check the Twilio setup without making a call.

Usage (backend folder, .venv active, settings in backend/.env):
    python send_test_message.py 9876543210
"""
import sys

from dotenv import load_dotenv

load_dotenv()  # before importing notify, which reads settings at import time

import notify  # noqa: E402

if len(sys.argv) != 2:
    sys.exit("Usage: python send_test_message.py <phone number>")

phone = "".join(c for c in sys.argv[1] if c.isdigit())
print(f"Channel: {notify.CHANNEL} | sending to {notify.to_e164(phone)} ...")
try:
    sid = notify.send_now(phone, f"Hi from {notify.BUSINESS_NAME}! Your booking confirmations are set up.")
    print(f"Sent. Twilio message id: {sid}. Check WhatsApp on that phone.")
except Exception as e:
    sys.exit(f"Not sent. {e}")