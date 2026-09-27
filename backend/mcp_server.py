"""Glow Studio MCP server (Step 6).

Exposes the booking system as standard MCP tools, so any MCP client can use it:
the voice agent, Claude Desktop, or other AI apps. It's a thin layer over the REST API
(main.py), so all business rules stay in one place.

Two tool groups (least privilege):
  CALLER tools  - what a customer on the phone may do (the voice agent uses only these)
  STAFF tools   - salon owner views with customer names/numbers (for Claude Desktop)

Run (the REST API must be running first):
  python mcp_server.py            -> HTTP at http://127.0.0.1:8001/mcp  (for the voice agent)
  python mcp_server.py --stdio    -> stdio, for Claude Desktop to launch itself

NOTE: there is no authentication yet. The agent's allowed_tools list only filters what the
agent *sees*; anyone who can reach this server can call every tool. Keep it on localhost
until auth is added (Phase 5).
"""
import json
import os
import sys

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

API_URL = os.getenv("BOOKING_API_URL", "http://127.0.0.1:8000")
MCP_PORT = int(os.getenv("MCP_PORT", "8001"))

mcp = FastMCP(
    "Glow Studio",
    instructions=(
        "Booking system for Glow Studio, a hair and beauty salon with one chair. "
        "Dates are YYYY-MM-DD, times are 24-hour HH:MM in the salon's local time, "
        "on 15-minute marks. Open Monday to Saturday, 10:00 to 19:00."
    ),
    host="127.0.0.1",
    port=MCP_PORT,
)

CALLER_TOOLS = [
    "list_services", "check_availability", "check_back_to_back", "book_appointment",
    "find_appointments", "reschedule_appointment", "cancel_appointment",
    "request_human_callback",
]


# ---------- helpers ----------

async def _api(method: str, path: str, **kw) -> tuple[bool, object]:
    try:
        async with httpx.AsyncClient(base_url=API_URL, timeout=5) as client:
            r = await client.request(method, path, **kw)
    except httpx.HTTPError:
        return False, "The booking system is unreachable right now. Offer a callback instead."
    if r.is_success:
        return True, r.json()
    try:
        detail = r.json().get("detail", r.text)
    except ValueError:
        detail = r.text
    return False, str(detail)


async def _service_id(name: str) -> tuple[int | None, str | None]:
    ok, services = await _api("GET", "/services")
    if not ok:
        return None, str(services)
    n = name.lower().strip()
    for s in services:
        if s["name"].lower() == n:
            return s["id"], None
    for s in services:
        sn = s["name"].lower()
        if n in sn or sn in n or n.replace("color", "colour") == sn:
            return s["id"], None
    return None, f"No service called '{name}'. Use list_services."


def _confirmation(data: dict) -> str:
    if data.get("confirmation_sent"):
        return "A confirmation message has been sent to the customer's phone."
    return "No confirmation message was sent."


# ---------- CALLER tools ----------

@mcp.tool()
async def list_services() -> str:
    """List the salon's services with duration in minutes and price."""
    ok, data = await _api("GET", "/services")
    return json.dumps(data) if ok else f"Error: {data}"


@mcp.tool()
async def check_availability(service_name: str, day: str) -> str:
    """Get free start times for ONE service on a day. For two or more services on the
    same day, use check_back_to_back instead.

    Args:
        service_name: Name of the service, e.g. "Haircut".
        day: Date as YYYY-MM-DD.
    """
    sid, err = await _service_id(service_name)
    if err:
        return f"Error: {err}"
    ok, data = await _api("GET", "/availability", params={"service_id": sid, "day": day})
    if not ok:
        return f"Error: {data}"
    if data["closed"]:
        return f"The salon is closed on {day}."
    return json.dumps(data) if data["slots"] else f"No free slots for {data['service']} on {day}."


@mcp.tool()
async def check_back_to_back(service_names: list[str], day: str) -> str:
    """Find times when several services can be done one after another on the same day
    (one chair, so they can't overlap). Book each service at exactly the start time given.

    Args:
        service_names: Services in the order they'll be done, e.g. ["Manicure", "Hair Colour"].
        day: Date as YYYY-MM-DD.
    """
    ids = []
    for name in service_names:
        sid, err = await _service_id(name)
        if err:
            return f"Error: {err}"
        ids.append(sid)
    ok, data = await _api("GET", "/availability/back-to-back",
                          params={"day": day, "service_ids": ids})
    if not ok:
        return f"Error: {data}"
    if data["closed"]:
        return f"The salon is closed on {day}."
    if not data["options"]:
        return (f"No back-to-back times for {', '.join(data['services'])} on {day} "
                "in this order. Try the other order or another day.")
    return json.dumps(data)


@mcp.tool()
async def book_appointment(service_name: str, day: str, time: str,
                           customer_name: str, phone: str) -> str:
    """Book an appointment. Only call after the customer has confirmed all details.

    Args:
        service_name: Name of the service.
        day: Date as YYYY-MM-DD.
        time: Start time as 24-hour HH:MM.
        customer_name: Customer's name.
        phone: Customer's phone number.
    """
    sid, err = await _service_id(service_name)
    if err:
        return f"Error: {err}"
    ok, data = await _api("POST", "/appointments", json={
        "service_id": sid, "customer_name": customer_name,
        "phone": phone, "start_time": f"{day}T{time}",
    })
    return f"Booked. {_confirmation(data)} Details: {json.dumps(data)}" if ok else f"Error: {data}"


@mcp.tool()
async def find_appointments(phone: str) -> str:
    """Look up a customer's upcoming appointments by phone number.

    Args:
        phone: The phone number used when booking.
    """
    ok, data = await _api("GET", "/appointments", params={"phone": phone})
    if not ok:
        return f"Error: {data}"
    return json.dumps(data) if data else "No upcoming appointments for that number."


@mcp.tool()
async def reschedule_appointment(appointment_id: int, new_day: str, new_time: str) -> str:
    """Move an existing appointment. Only call after the customer confirms.

    Args:
        appointment_id: ID from find_appointments.
        new_day: New date as YYYY-MM-DD.
        new_time: New start time as 24-hour HH:MM.
    """
    ok, data = await _api("PATCH", f"/appointments/{appointment_id}",
                          json={"start_time": f"{new_day}T{new_time}"})
    return f"Rescheduled. {_confirmation(data)} Details: {json.dumps(data)}" if ok else f"Error: {data}"


@mcp.tool()
async def cancel_appointment(appointment_id: int) -> str:
    """Cancel an appointment. Only call after the customer confirms.

    Args:
        appointment_id: ID from find_appointments.
    """
    ok, data = await _api("POST", f"/appointments/{appointment_id}/cancel")
    return f"Cancelled. {_confirmation(data)}" if ok else f"Error: {data}"


@mcp.tool()
async def request_human_callback(customer_name: str, phone: str, reason: str,
                                 urgent: bool = False) -> str:
    """Ask a staff member to call the customer back. Only call after the customer has
    confirmed their name, number and the reason.

    Args:
        customer_name: Customer's name.
        phone: Customer's phone number.
        reason: One short sentence on what they need.
        urgent: True only for health or safety issues, or a same-day problem.
    """
    ok, data = await _api("POST", "/callbacks", json={
        "customer_name": customer_name, "phone": phone, "reason": reason,
        "urgency": "urgent" if urgent else "normal",
    })
    if not ok:
        return f"Error: {data}"
    return (f"Callback request saved (#{data['id']}). A team member will call back during "
            "opening hours" + (", as a priority." if urgent else "."))


# ---------- STAFF tools (not given to the voice agent) ----------

@mcp.tool()
async def get_day_schedule(day: str) -> str:
    """STAFF ONLY. Full schedule for a day, including customer names and phone numbers.

    Args:
        day: Date as YYYY-MM-DD.
    """
    ok, data = await _api("GET", "/schedule", params={"day": day})
    if not ok:
        return f"Error: {data}"
    return json.dumps(data) if data else f"No appointments on {day}."


@mcp.tool()
async def list_callbacks(status: str = "open") -> str:
    """STAFF ONLY. Callback requests, urgent first.

    Args:
        status: "open", "done" or "all".
    """
    ok, data = await _api("GET", "/callbacks", params={"status": status})
    if not ok:
        return f"Error: {data}"
    return json.dumps(data) if data else f"No {status} callback requests."


@mcp.tool()
async def complete_callback(callback_id: int) -> str:
    """STAFF ONLY. Mark a callback request as handled.

    Args:
        callback_id: ID from list_callbacks.
    """
    ok, data = await _api("POST", f"/callbacks/{callback_id}/done")
    return f"Callback #{callback_id} marked done." if ok else f"Error: {data}"


if __name__ == "__main__":
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")  # Claude Desktop starts this process itself
    else:
        print(f"Glow Studio MCP server on http://127.0.0.1:{MCP_PORT}/mcp (API: {API_URL})")
        mcp.run(transport="streamable-http")