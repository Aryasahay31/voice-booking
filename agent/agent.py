"""Glow Studio voice receptionist, built on LiveKit Agents 1.x.
Persona, greeting and instructions live in prompts.py.

Run:  python agent.py console   (talk to it in your terminal)
      python agent.py dev       (connect to your LiveKit project)
"""
import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    ErrorEvent,
    RunContext,
    TurnHandlingOptions,
    UserStateChangedEvent,
    function_tool,
    get_job_context,
)
from livekit.agents import mcp
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import silero

load_dotenv()  # before importing prompts, which reads .env settings at import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from call_metrics import CallMetrics  # noqa: E402
from models import build_llm, build_stt, build_tts, watch_availability  # noqa: E402
from prompts import (  # noqa: E402
    AWAY_CHECKIN_INSTRUCTIONS,
    AWAY_GOODBYE,
    END_CALL_DESCRIPTION,
    END_CALL_GOODBYE,
    GREETING,
    build_instructions,
)

logger = logging.getLogger("booking-agent")

API_URL = os.getenv("BOOKING_API_URL", "http://127.0.0.1:8000")
# Must match the backend's BACKEND_API_KEY once it's deployed (empty = local, no key).
_api_key = os.getenv("BACKEND_API_KEY", "").strip()
API_HEADERS = {"X-API-Key": _api_key} if _api_key else {}

# ---- conversation handling (Step 2) ----
# If the backend is down, callback requests are saved here so nobody gets lost.
PENDING_CALLBACKS_FILE = Path(__file__).parent / "pending_callbacks.jsonl"

# ---- tool source (Step 6) ----
# local: booking tools built into this file.  mcp: booking tools from backend/mcp_server.py
TOOLS_SOURCE = os.getenv("TOOLS_SOURCE", "local").lower()
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8001/mcp")
# Least privilege: the voice agent only gets caller tools, never the staff ones.
MCP_CALLER_TOOLS = [
    "list_services", "check_availability", "check_back_to_back", "book_appointment",
    "find_appointments", "reschedule_appointment", "cancel_appointment",
    "request_human_callback",
]

# ---- guardrails in code (Step 7: prompt rules alone failed the evals) ----
PHONE_DIGITS = int(os.getenv("PHONE_DIGITS", "10"))
COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "91")


def phone_problem(phone: str) -> str | None:
    """Return an error message if the number can't be right, else None."""
    d = "".join(c for c in phone if c.isdigit())
    if len(d) == PHONE_DIGITS or (d.startswith(COUNTRY_CODE) and len(d) == len(COUNTRY_CODE) + PHONE_DIGITS):
        return None
    return (f"Error: that phone number has {len(d)} digits, but numbers here have {PHONE_DIGITS}. "
            "Don't use it. Ask the caller to say their number again, slowly.")


_NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                 "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def spoken_digits(text: str) -> str:
    """All digits in a message, whether written as digits or words:
    'six two zero, 2 7 6' -> '620276'."""
    tokens = re.findall(r"[a-z]+|\d", text.lower())
    return "".join(_NUMBER_WORDS.get(t, t if t.isdigit() else "") for t in tokens)


# ---- latency (Step 5) ----
FILLER_AFTER_S = 1.5  # if a booking-system request takes longer than this, say a filler
FILLERS = ["One moment, still checking.", "Bear with me a second.", "Just a sec, pulling that up."]

USER_AWAY_TIMEOUT = 12.0  # seconds of mutual silence before the caller counts as "away"
MAX_CHECKINS = 2          # "are you still there?" attempts before hanging up
CHECKIN_WAIT = 8.0        # seconds to wait after each check-in


class VoiceAssistant(Agent):
    """Voice behaviour that belongs in the agent whatever the tool source: the persona,
    hold requests, recording consent and ending the call. Booking tools come either from
    BookingAssistant below (TOOLS_SOURCE=local) or from the MCP server (TOOLS_SOURCE=mcp)."""

    def __init__(self, extra_tools: list | None = None) -> None:
        end_call = EndCallTool(
            extra_description=END_CALL_DESCRIPTION,
            end_instructions=END_CALL_GOODBYE,
            delete_room=True,  # disconnect everyone in the room when the call ends
        )
        super().__init__(
            instructions=build_instructions(),
            tools=[*end_call.tools, *(extra_tools or [])],
        )
        self.hold_until = 0.0  # monotonic time until which silence is expected (caller asked to wait)
        self.recording_consent: bool | None = None  # None = not discussed; False = don't save transcript

    @property
    def user_turns(self) -> int:
        """How many messages the caller has sent so far in this conversation.
        Counted from the chat history (not the on_user_turn_completed hook) because typed
        input and evals skip the voice turn pipeline, so the hook never fires for them."""
        return sum(
            1 for item in self.chat_ctx.items
            if item.type == "message" and item.role == "user"
        )

    @function_tool
    async def wait_for_caller(self, context: RunContext, seconds: int = 60) -> str:
        """Use when the caller asks you to hold on, wait, or give them a moment
        (for example to check their calendar). Pauses the "are you still there?" check-ins.

        Args:
            seconds: Roughly how long they need, between 10 and 180. Default 60.
        """
        seconds = max(10, min(seconds, 180))
        self.hold_until = time.monotonic() + seconds
        logger.info("caller asked to wait; pausing silence check-ins for %ss", seconds)
        return "Okay. Tell the caller to take their time in a few words, then stay quiet until they speak."

    @function_tool
    async def set_recording_consent(self, context: RunContext, consented: bool) -> str:
        """Record whether the caller agrees to the call being recorded and transcribed.

        Args:
            consented: True if the caller agreed, False if they declined.
        """
        self.recording_consent = consented
        logger.info("recording consent set to %s", consented)
        if consented:
            return "Noted: the caller agreed to recording."
        return "Noted: this call will not be recorded or transcribed. Reassure the caller and carry on."



class BookingAssistant(VoiceAssistant):
    """TOOLS_SOURCE=local: booking tools built into the agent. Compared with MCP they can do
    voice-specific extras: filler speech when the backend is slow, live calendar updates to
    the website, and saving callbacks to a local file when the backend is down."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__()
        self.http = http
        self._services: list[dict] | None = None
        self._staged: dict[str, int] = {}  # action -> caller turn when it was read back

    def _confirm_gate(self, action_key: str, summary: str,
                      must_mention: list[str] | None = None) -> str | None:
        """Two-step confirmation, enforced in code rather than trusted to the prompt.

        The action goes ahead (returns None) if EITHER:
          - it was staged by an earlier call and the caller has spoken since, OR
          - Maya's message just before the caller's latest reply already read back the
            key details (must_mention, e.g. name + phone digits), so the caller has
            already confirmed them. This avoids asking twice.
        Otherwise it's staged, and Maya gets instructions to read the details back."""
        staged_at = self._staged.get(action_key)
        if staged_at is not None and self.user_turns > staged_at:
            del self._staged[action_key]
            return None
        if staged_at is None and must_mention and self._previous_reply_mentions(must_mention):
            return None
        self._staged[action_key] = self.user_turns
        return (f"NOT DONE YET. Read this back to the caller and ask them to confirm: {summary}. "
                "Only if they clearly say yes, call this tool again with exactly the same details.")

    def _previous_reply_mentions(self, required: list[str]) -> bool:
        """Did Maya's last message before the caller's latest reply contain every required
        item? Digit strings are matched against spoken digits ('six two zero' counts)."""
        items = self.chat_ctx.items
        last_user = max((i for i, it in enumerate(items)
                         if it.type == "message" and it.role == "user"), default=None)
        if last_user is None:
            return False
        prev = next((it for it in reversed(items[:last_user])
                     if it.type == "message" and it.role == "assistant"), None)
        if prev is None or not prev.text_content:
            return False
        text = prev.text_content
        said_digits = spoken_digits(text)
        for req in required:
            if req.isdigit():
                if req not in said_digits:
                    return False
            elif req.lower() not in text.lower():
                return False
        return True

    async def _filler_if_slow(self) -> None:
        """Say a short filler if the booking system is slow, so the caller doesn't hear dead air."""
        await asyncio.sleep(FILLER_AFTER_S)
        try:
            self.session.say(random.choice(FILLERS), add_to_chat_ctx=False)
            logger.info("booking system slow (>%.1fs): played filler", FILLER_AFTER_S)
        except Exception as e:  # e.g. no active session in tests
            logger.debug("filler skipped: %s", e)

    async def _call(self, method: str, path: str, **kw) -> tuple[bool, object]:
        filler = asyncio.create_task(self._filler_if_slow())
        try:
            return await self._call_api(method, path, **kw)
        finally:
            filler.cancel()

    async def _call_api(self, method: str, path: str, **kw) -> tuple[bool, object]:
        # Reads (GET) are safe to retry once; writes are not, to avoid double bookings.
        attempts = 2 if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                r = await self.http.request(method, path, **kw)
                break
            except httpx.HTTPError as e:
                # repr() shows the error type, e.g. ReadTimeout(''), ConnectError(...)
                logger.error("booking API %s %s failed (attempt %d/%d): %r",
                             method, path, attempt, attempts, e)
                if attempt == attempts:
                    return False, ("The booking system is unreachable right now. "
                                   "Offer to take a callback request instead.")
        if r.is_success:
            return True, r.json()
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        # Make backend refusals visible (e.g. 401 = API key mismatch). 4xx like 409 "slot
        # taken" are normal conversation, so only auth/server problems are warnings.
        level = logging.WARNING if r.status_code in (401, 403) or r.status_code >= 500 else logging.INFO
        logger.log(level, "booking API %s %s -> %d: %s", method, path, r.status_code, detail)
        if r.status_code in (401, 403):
            logger.warning("check that BACKEND_API_KEY is identical in agent/.env and backend/.env, "
                           "and restart the backend after editing its .env")
        return False, str(detail)

    async def _find_service(self, name: str) -> dict | None:
        if self._services is None:
            ok, data = await self._call("GET", "/services")
            if not ok:
                return None
            self._services = data
        n = name.lower().strip()
        for s in self._services:
            if s["name"].lower() == n:
                return s
        for s in self._services:  # loose match: "hair color" -> "Hair Colour"
            sn = s["name"].lower()
            if n in sn or sn in n or n.replace("color", "colour") == sn:
                return s
        return None

    @staticmethod
    def _confirmation_note(data: dict) -> str:
        if data.get("confirmation_sent"):
            return "A confirmation message has been sent to the caller's phone."
        return "No confirmation message was sent."

    async def _notify_calendar(self, day: str) -> None:
        """Tell the website (Phase 2) to refresh its calendar for this day."""
        try:
            room = get_job_context().room
            await room.local_participant.publish_data(
                json.dumps({"type": "calendar_updated", "day": day}).encode(),
                topic="calendar",
                reliable=True,
            )
        except Exception as e:  # no room in console mode, etc.
            logger.debug("calendar notify skipped: %s", e)

    # ---------- tools ----------

    @function_tool
    async def request_human_callback(
        self,
        context: RunContext,
        customer_name: str,
        phone: str,
        reason: str,
        urgent: bool = False,
    ) -> str:
        """Ask a staff member to call the customer back. Use when the caller wants a human,
        or for anything you can't or shouldn't handle (see Escalation rules).
        Only call after the caller has confirmed their name, number and the reason.

        Args:
            customer_name: Caller's name.
            phone: Caller's phone number.
            reason: One short sentence on what they need, in your own words.
            urgent: True only for health or safety issues, or a same-day problem.
        """
        if problem := phone_problem(phone):
            return problem
        payload = {
            "customer_name": customer_name,
            "phone": phone,
            "reason": reason,
            "urgency": "urgent" if urgent else "normal",
        }
        ok, data = await self._call("POST", "/callbacks", json=payload)
        if ok:
            return (f"Callback request saved (#{data['id']}). Tell the caller a team member "
                    "will call them back during opening hours"
                    + (", as a priority." if urgent else "."))
        if isinstance(data, str) and "unreachable" in data:
            # Backend down: keep the request locally rather than lose the customer.
            record = {**payload, "saved_at": datetime.now(timezone.utc).isoformat()}
            try:
                with PENDING_CALLBACKS_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                logger.error("backend down: callback saved locally to %s", PENDING_CALLBACKS_FILE.name)
                return "Callback request noted. Tell the caller a team member will call them back."
            except OSError:
                logger.exception("could not save callback locally: %s", record)
                return "Error: could not save the request. Ask the caller to call back later."
        return f"Error: {data}"

    @function_tool
    async def list_services(self, context: RunContext) -> str:
        """List the salon's services with duration in minutes and price."""
        ok, data = await self._call("GET", "/services")
        return json.dumps(data) if ok else f"Error: {data}"

    @function_tool
    async def check_availability(self, context: RunContext, service_name: str, day: str) -> str:
        """Get free start times for a service on a given day.

        Args:
            service_name: Name of the service, e.g. "Haircut".
            day: Date as YYYY-MM-DD.
        """
        svc = await self._find_service(service_name)
        if not svc:
            if self._services is None:
                return "Error: The booking system is unreachable right now."
            return f"Error: no service called '{service_name}'. Use list_services."
        ok, data = await self._call(
            "GET", "/availability", params={"service_id": svc["id"], "day": day}
        )
        if not ok:
            return f"Error: {data}"
        if data["closed"]:
            return f"The salon is closed on {day}."
        if not data["slots"]:
            return f"No free slots for {svc['name']} on {day}."
        return json.dumps(data)

    @function_tool
    async def check_back_to_back(
        self, context: RunContext, service_names: list[str], day: str
    ) -> str:
        """Find times when several services can be done one after another on the same day.
        The salon has one chair, so services can't overlap. Use this whenever the caller
        wants two or more services on one day, instead of check_availability.

        Args:
            service_names: Services in the order they'll be done, e.g. ["Manicure", "Hair Colour"].
            day: Date as YYYY-MM-DD.
        """
        # Time arithmetic is done in code (the backend), not by the LLM, which got it wrong
        # in testing. The MCP server uses the same endpoint, so both agree.
        ids = []
        for name in service_names:
            svc = await self._find_service(name)
            if not svc:
                if self._services is None:
                    return "Error: The booking system is unreachable right now."
                return f"Error: no service called '{name}'. Use list_services."
            ids.append(svc["id"])
        ok, data = await self._call(
            "GET", "/availability/back-to-back", params={"day": day, "service_ids": ids}
        )
        if not ok:
            return f"Error: {data}"
        if data["closed"]:
            return f"The salon is closed on {day}."
        if not data["options"]:
            return (f"No back-to-back times for {', '.join(data['services'])} on {day} "
                    "in this order. Try the other order or another day.")
        return json.dumps(data)

    @function_tool
    async def book_appointment(
        self,
        context: RunContext,
        service_name: str,
        day: str,
        time: str,
        customer_name: str,
        phone: str,
    ) -> str:
        """Book an appointment. Only call after the caller has confirmed all details.

        Args:
            service_name: Name of the service.
            day: Date as YYYY-MM-DD.
            time: Start time as 24-hour HH:MM.
            customer_name: Caller's name.
            phone: Caller's phone number.
        """
        if problem := phone_problem(phone):
            return problem
        svc = await self._find_service(service_name)
        if not svc:
            if self._services is None:
                return "Error: The booking system is unreachable right now."
            return f"Error: no service called '{service_name}'. Use list_services."
        digits_ = "".join(c for c in phone if c.isdigit())
        if wait := self._confirm_gate(
            f"book|{svc['id']}|{day}|{time}|{customer_name.strip().lower()}|{digits_}",
            f"{svc['name']} on {day} at {time} for {customer_name}, phone {' '.join(digits_)}",
            must_mention=[customer_name.strip(), digits_[-PHONE_DIGITS:]],
        ):
            return wait
        ok, data = await self._call(
            "POST",
            "/appointments",
            json={
                "service_id": svc["id"],
                "customer_name": customer_name,
                "phone": phone,
                "start_time": f"{day}T{time}",
            },
        )
        if not ok:
            return f"Error: {data}"
        await self._notify_calendar(day)
        return f"Booked. {self._confirmation_note(data)} Details: {json.dumps(data)}"

    @function_tool
    async def find_appointments(self, context: RunContext, phone: str) -> str:
        """Look up a caller's upcoming appointments by phone number.

        Args:
            phone: The phone number used when booking.
        """
        if problem := phone_problem(phone):
            return problem
        ok, data = await self._call("GET", "/appointments", params={"phone": phone})
        if not ok:
            return f"Error: {data}"
        return json.dumps(data) if data else "No upcoming appointments for that number."

    @function_tool
    async def reschedule_appointment(
        self, context: RunContext, appointment_id: int, new_day: str, new_time: str
    ) -> str:
        """Move an existing appointment. Only call after the caller confirms.

        Args:
            appointment_id: ID from find_appointments.
            new_day: New date as YYYY-MM-DD.
            new_time: New start time as 24-hour HH:MM.
        """
        if wait := self._confirm_gate(
            f"move|{appointment_id}|{new_day}|{new_time}",
            f"move appointment {appointment_id} to {new_day} at {new_time}",
        ):
            return wait
        ok, data = await self._call(
            "PATCH", f"/appointments/{appointment_id}",
            json={"start_time": f"{new_day}T{new_time}"},
        )
        if not ok:
            return f"Error: {data}"
        await self._notify_calendar(new_day)
        return f"Rescheduled. {self._confirmation_note(data)} Details: {json.dumps(data)}"

    @function_tool
    async def cancel_appointment(self, context: RunContext, appointment_id: int) -> str:
        """Cancel an appointment. Only call after the caller confirms.

        Args:
            appointment_id: ID from find_appointments.
        """
        if wait := self._confirm_gate(f"cancel|{appointment_id}", f"cancel appointment {appointment_id}"):
            return wait
        ok, data = await self._call("POST", f"/appointments/{appointment_id}/cancel")
        if not ok:
            return f"Error: {data}"
        await self._notify_calendar(data["start_time"][:10])
        return f"Cancelled. {self._confirmation_note(data)}"


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    # Building the client loads SSL certificates, which took ~5s on your machine and froze
    # the audio loop. Doing it in a worker thread keeps the agent responsive.
    http = await asyncio.to_thread(
        lambda: httpx.AsyncClient(base_url=API_URL, timeout=5, headers=API_HEADERS)
    )
    ctx.add_shutdown_callback(http.aclose)

    # Each stage has a primary + backup provider (see models.py).
    # VAD was loaded once in prewarm(); providers are built in a thread because creating
    # six HTTP clients (with SSL setup) froze the event loop for ~3s on startup.
    vad_model = ctx.proc.userdata["vad"]
    stt_model, llm_model, tts_model = await asyncio.to_thread(
        lambda: (build_stt(vad_model), build_llm(), build_tts())
    )
    watch_availability(stt_model, llm_model, tts_model)

    session = AgentSession(
        stt=stt_model,
        llm=llm_model,
        tts=tts_model,
        vad=vad_model,
        turn_handling=TurnHandlingOptions(
            # The audio turn detector (semantic end-of-turn model) is the default, so we
            # only tune timing. "dynamic" learns this caller's pause style: quick for
            # snappy talkers, more patient for slow ones (e.g. reading out a phone number).
# Tuned from call metrics:
            # - min 0.6: Deepgram's final transcript sometimes arrived after a 0.5s commit.
            # - max 1.5: short answers ("Yes.", "Okay.") often get a low end-of-turn score
            #   and were waiting the full maximum.
            # - "fixed", not "dynamic": dynamic mode learned from callers' long thinking
            #   pauses and crept the minimum up to 1.3s, slowing every later reply.
            endpointing={"mode": "fixed", "min_delay": 0.6, "max_delay": 1.5},
            # Adaptive = a model decides whether overlapping speech is a real interruption
            # or just "mm-hmm" / "okay" backchanneling. If a noise stops the agent and no
            # words follow, it resumes where it left off.
            interruption={"mode": "adaptive", "resume_false_interruption": True},
        ),
        user_away_timeout=USER_AWAY_TIMEOUT,
    )
    if TOOLS_SOURCE == "mcp":
        logger.info("booking tools from MCP server %s (caller tools only)", MCP_URL)
        # MCPToolset connects to the server when the session starts and fetches its tools.
        # allowed_tools hides the staff tools from the agent (least privilege).
        agent = VoiceAssistant(extra_tools=[
            mcp.MCPToolset(
                id="glow_studio",
                mcp_server=mcp.MCPServerHTTP(
                    url=MCP_URL, allowed_tools=MCP_CALLER_TOOLS, timeout=10
                ),
            ),
        ])
    else:
        logger.info("booking tools: local (built into agent.py)")
        agent = BookingAssistant(http)

    # ---- silence handling: check in, then hang up politely ----
    away_task: asyncio.Task | None = None

    async def handle_away() -> None:
        # If the caller asked us to wait, respect that before checking in.
        remaining = agent.hold_until - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        for attempt in range(MAX_CHECKINS):
            logger.info("caller silent: check-in %d/%d", attempt + 1, MAX_CHECKINS)
            await session.generate_reply(instructions=AWAY_CHECKIN_INSTRUCTIONS[attempt])
            await asyncio.sleep(CHECKIN_WAIT)
        logger.info("no response after check-ins; ending call")
        await session.say(AWAY_GOODBYE, allow_interruptions=False)
        session.shutdown()

    @session.on("user_state_changed")
    def on_user_state_changed(ev: UserStateChangedEvent) -> None:
        nonlocal away_task
        if ev.new_state == "away":
            if away_task is None or away_task.done():
                away_task = asyncio.create_task(handle_away())
        elif away_task is not None:
            # Caller is back (speaking or listening again): cancel pending check-ins.
            away_task.cancel()
            away_task = None

    # ---- metrics: per-turn latency + usage/cost, saved at the end of the call ----
    call_metrics = CallMetrics()
    session.on("metrics_collected", call_metrics.on_metrics)
    session.on("user_state_changed", call_metrics.on_user_state)
    session.on("agent_state_changed", call_metrics.on_agent_state)

    async def save_call_log() -> None:
        consent = agent.recording_consent  # False = caller declined; transcript not saved
        transcript = None if consent is False else session.history.to_dict()
        await asyncio.to_thread(call_metrics.finish, transcript, consent)

    ctx.add_shutdown_callback(save_call_log)

    # ---- errors: log them; fallbacks handle most, unrecoverable ones end the session ----
    @session.on("error")
    def on_error(ev: ErrorEvent) -> None:
        if getattr(ev.error, "recoverable", False):
            logger.warning("recoverable error from %s: %s", type(ev.source).__name__, ev.error)
        else:
            logger.error("UNRECOVERABLE error from %s: %s (all providers failed?)",
                         type(ev.source).__name__, ev.error)

    await session.start(room=ctx.room, agent=agent)
    # Fixed greeting with the AI + recording disclosure. Not interruptible, so the caller
    # always hears the full disclosure before the conversation starts.
    await session.say(GREETING, allow_interruptions=False)


def prewarm(proc: agents.JobProcess) -> None:
    """Runs once per worker process, before any call arrives: load heavy models here
    so the first call doesn't wait for them."""
    proc.userdata["vad"] = silero.VAD.load()


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))