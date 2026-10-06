"""List the WhatsApp/message templates your Twilio account can use, with their Content SIDs.
Useful when the console's Template Builder page is locked on a trial account.

Usage (backend folder, .venv active):  python list_templates.py
Prints no secrets: only template IDs, names and text.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from twilio.base.exceptions import TwilioException, TwilioRestException  # noqa: E402
from twilio.rest import Client  # noqa: E402

try:
    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    templates = client.content.v1.contents.list(limit=50)
except KeyError as e:
    sys.exit(f"Missing setting in .env: {e}")
except TwilioRestException as e:
    sys.exit(f"Twilio refused (error {e.code}): {e.msg}")
except TwilioException as e:  # list requests report refusals this way
    sys.exit(f"Twilio refused to list templates (often a trial-account limit): {e}")

if not templates:
    sys.exit("No templates are visible to this account through the API.")

for t in templates:
    body = ""
    for kind in (t.types or {}).values():
        if isinstance(kind, dict) and kind.get("body"):
            body = kind["body"]
            break
    print(f"{t.sid}  |  {t.friendly_name}  |  {t.language}\n    {body}\n")