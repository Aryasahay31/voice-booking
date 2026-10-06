"""Salon booking API: the single source of truth.
Both the voice agent and (later) the website talk to this."""
import logging
import os
import secrets
import uuid
from collections import defaultdict, deque
from time import monotonic  # (plain "time" is already taken by datetime.time below)
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()  # must run before importing notify, which reads settings at import time

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import db
import notify
from livekit import api as livekit_api

TZ = ZoneInfo(os.getenv("BUSINESS_TZ", "UTC"))
OPEN_AT = time(10, 0)
CLOSE_AT = time(19, 0)
SLOT_STEP_MIN = 15  # 15-min steps so back-to-back services line up (e.g. 45-min manicure)
CLOSED_WEEKDAYS = {6}  # 6 = Sunday

app = FastAPI(title="Glow Studio Booking API")
# CORS: which WEBSITES may call this API from a visitor's browser. Without a limit, any site
# could embed a "Talk to Shanaya" button that spends our provider credits. Set ALLOWED_ORIGINS
# on Render to the real website, e.g. "https://voice-booking-ruby.vercel.app".
# (CORS only restrains browsers; scripts can still call /token, which is why it's rate-limited.)
ALLOWED_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "X-API-Key"],
)
db.init()

# ---------- security: API key ----------
# Once the backend is on the internet, anyone who finds the URL could book, cancel, or read
# customer details. So every endpoint requires the X-API-Key header, except a few that
# expose no personal data (the Phase 2 website will read these from the browser).
API_KEY = os.getenv("BACKEND_API_KEY", "").strip()
PUBLIC_PATHS = {"/health", "/health/db", "/services", "/calendar", "/token", "/docs", "/openapi.json"}
log = logging.getLogger("uvicorn.error")
if not API_KEY:
    log.warning("BACKEND_API_KEY is not set: API is OPEN (fine locally, never in the cloud)")


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if (API_KEY and request.method != "OPTIONS"
            and request.url.path not in PUBLIC_PATHS
            and not secrets.compare_digest(request.headers.get("x-api-key", ""), API_KEY)):
        return JSONResponse({"detail": "Missing or invalid API key"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health():
    """Lightweight check for Render's health checks and keep-alive pings.
    Deliberately does NOT touch the database: Neon's free plan sleeps after 5 idle minutes
    and has a monthly compute allowance, so frequent pings must not wake it up."""
    return {"ok": True, "database": db.ENGINE}


@app.get("/health/db")
def health_db():
    """Deep check: confirms the database actually answers. Use it by hand, not for pings."""
    with db.connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True, "database": db.ENGINE, "db_reachable": True}


# ---------- helpers ----------

def fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def normalize_dt(dt: datetime) -> datetime:
    if dt.tzinfo:
        dt = dt.astimezone(TZ).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 6:
        raise HTTPException(400, "That phone number looks too short")
    return digits


def get_service(conn, service_id: int):
    row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Service not found")
    return row


def get_appointment(conn, appt_id: int) -> dict:
    row = conn.execute(
        """SELECT a.*, s.name AS service, s.price
           FROM appointments a JOIN services s ON s.id = a.service_id
           WHERE a.id = ?""",
        (appt_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Appointment not found")
    return dict(row)


def is_free(conn, start: datetime, end: datetime, exclude_id: int | None = None) -> bool:
    # Two intervals overlap when each starts before the other ends.
    q = """SELECT 1 FROM appointments
           WHERE status = 'booked' AND start_time < ? AND end_time > ?"""
    params: list = [fmt(end), fmt(start)]
    if exclude_id is not None:
        q += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(q, params).fetchone() is None


def validate_slot(start: datetime, duration_min: int) -> datetime:
    end = start + timedelta(minutes=duration_min)
    if start.weekday() in CLOSED_WEEKDAYS:
        raise HTTPException(400, "The salon is closed on Sundays")
    if start.time() < OPEN_AT or end.time() > CLOSE_AT or end.date() != start.date():
        raise HTTPException(
            400, f"Outside opening hours ({OPEN_AT:%H:%M} to {CLOSE_AT:%H:%M})"
        )
    if start.minute % SLOT_STEP_MIN:
        raise HTTPException(400, "Start times must be on a 15-minute mark (:00, :15, :30, :45)")
    if start <= now_local():
        raise HTTPException(400, "That time is in the past")
    return end


# ---------- schemas ----------

class BookingIn(BaseModel):
    service_id: int
    customer_name: str = Field(min_length=1, max_length=80)
    phone: str
    start_time: datetime


class RescheduleIn(BaseModel):
    start_time: datetime


class CallbackIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=80)
    phone: str
    reason: str = Field(min_length=1, max_length=500)
    urgency: Literal["normal", "urgent"] = "normal"


# ---------- routes ----------

@app.get("/services")
def list_services():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM services ORDER BY id")]


@app.get("/availability")
def availability(service_id: int, day: date):
    with db.connect() as conn:
        svc = get_service(conn, service_id)
        if day.weekday() in CLOSED_WEEKDAYS:
            return {"day": day.isoformat(), "service": svc["name"], "closed": True, "slots": []}

        dur = timedelta(minutes=svc["duration_min"])
        close = datetime.combine(day, CLOSE_AT)
        now = now_local()
        slots, t = [], datetime.combine(day, OPEN_AT)
        while t + dur <= close:
            if t > now and is_free(conn, t, t + dur):
                slots.append(t.strftime("%H:%M"))
            t += timedelta(minutes=SLOT_STEP_MIN)
        return {"day": day.isoformat(), "service": svc["name"], "closed": False, "slots": slots}


@app.get("/availability/back-to-back")
def back_to_back(day: date, service_ids: list[int] = Query(...)):
    """Start times where several services fit one after another (one chair, no overlaps).
    Order matters: service_ids=5&service_ids=2 means manicure first, then hair colour.
    Lives here (not in the agent or MCP server) so every client shares one implementation."""
    with db.connect() as conn:
        services = [get_service(conn, sid) for sid in service_ids]
    names = [s["name"] for s in services]
    if day.weekday() in CLOSED_WEEKDAYS:
        return {"day": day.isoformat(), "services": names, "closed": True, "options": []}
    free = [set(availability(s["id"], day)["slots"]) for s in services]

    # Each service's free slots already account for existing bookings, so a chain where
    # every service starts inside its own free slots never overlaps anything.
    options = []
    for first in sorted(free[0]):
        t, plan = datetime.strptime(first, "%H:%M"), []
        for svc, slots in zip(services, free):
            hhmm = t.strftime("%H:%M")
            if hhmm not in slots:
                break
            plan.append({"service": svc["name"], "start": hhmm})
            t += timedelta(minutes=svc["duration_min"])
        else:
            options.append({"plan": plan, "all_done_by": t.strftime("%H:%M")})
    return {"day": day.isoformat(), "services": names, "closed": False, "options": options}


@app.get("/schedule")
def schedule(day: date):
    """Staff view: the full day with customer names and numbers.
    NOTE: exposes personal data. Local use only until staff login exists (Phase 5)."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT a.id, s.name AS service, a.customer_name, a.phone, a.start_time, a.end_time
               FROM appointments a JOIN services s ON s.id = a.service_id
               WHERE a.status = 'booked' AND a.start_time LIKE ?
               ORDER BY a.start_time""",
            (f"{day.isoformat()}%",),
        )
        return [dict(r) for r in rows]


@app.post("/appointments", status_code=201)
def book(body: BookingIn, background: BackgroundTasks):
    start = normalize_dt(body.start_time)
    phone = normalize_phone(body.phone)
    with db.connect() as conn:
        svc = get_service(conn, body.service_id)
        end = validate_slot(start, svc["duration_min"])
        with db.transaction(conn):
            if not is_free(conn, start, end):
                raise HTTPException(409, "Sorry, that slot is already taken")
            new_id = conn.execute(
                """INSERT INTO appointments (service_id, customer_name, phone, start_time, end_time)
                   VALUES (?, ?, ?, ?, ?) RETURNING id""",
                (svc["id"], body.customer_name.strip(), phone, fmt(start), fmt(end)),
            ).fetchone()["id"]  # RETURNING works on both SQLite and Postgres
        appt = get_appointment(conn, new_id)
    background.add_task(notify.booking_confirmed, appt)
    return {**appt, "confirmation_sent": notify.enabled()}


@app.get("/appointments")
def find_by_phone(phone: str):
    """Upcoming bookings for a phone number (matched on the last 10 digits,
    so it works with or without a country code)."""
    p = normalize_phone(phone)
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT a.id, s.name AS service, a.customer_name, a.start_time, a.end_time
               FROM appointments a JOIN services s ON s.id = a.service_id
               WHERE a.phone LIKE ? AND a.status = 'booked' AND a.start_time >= ?
               ORDER BY a.start_time""",
            (f"%{p[-10:]}", fmt(now_local())),  # last 10 digits: with or without country code
        )
        return [dict(r) for r in rows]


@app.patch("/appointments/{appt_id}")
def reschedule(appt_id: int, body: RescheduleIn, background: BackgroundTasks):
    start = normalize_dt(body.start_time)
    with db.connect() as conn:
        appt = get_appointment(conn, appt_id)
        if appt["status"] != "booked":
            raise HTTPException(400, "That appointment was cancelled")
        svc = get_service(conn, appt["service_id"])
        end = validate_slot(start, svc["duration_min"])
        with db.transaction(conn):
            if not is_free(conn, start, end, exclude_id=appt_id):
                raise HTTPException(409, "Sorry, that slot is already taken")
            conn.execute(
                "UPDATE appointments SET start_time = ?, end_time = ? WHERE id = ?",
                (fmt(start), fmt(end), appt_id),
            )
        appt = get_appointment(conn, appt_id)
    background.add_task(notify.booking_rescheduled, appt)
    return {**appt, "confirmation_sent": notify.enabled()}


@app.post("/appointments/{appt_id}/cancel")
def cancel(appt_id: int, background: BackgroundTasks):
    with db.connect() as conn:
        appt = get_appointment(conn, appt_id)
        if appt["status"] == "cancelled":
            raise HTTPException(400, "That appointment is already cancelled")
        conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))
        appt = get_appointment(conn, appt_id)
    background.add_task(notify.booking_cancelled, appt)
    return {**appt, "confirmation_sent": notify.cancellation_messages_enabled()}


@app.get("/calendar")
def calendar(day: date):
    """Public view for the website's calendar: no names or phone numbers."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT a.id, s.name AS service, a.start_time, a.end_time
               FROM appointments a JOIN services s ON s.id = a.service_id
               WHERE a.status = 'booked' AND a.start_time LIKE ?
               ORDER BY a.start_time""",
            (f"{day.isoformat()}%",),
        )
        return [dict(r) for r in rows]


# ---------- escalation: callback requests ----------
# These endpoints expose customer phone numbers, so they require the API key (see above).
# A proper staff login comes in Phase 5.

@app.post("/callbacks", status_code=201)
def request_callback(body: CallbackIn, background: BackgroundTasks):
    phone = normalize_phone(body.phone)
    with db.connect() as conn:
        new_id = conn.execute(
            """INSERT INTO callback_requests (customer_name, phone, reason, urgency)
               VALUES (?, ?, ?, ?) RETURNING id""",
            (body.customer_name.strip(), phone, body.reason.strip(), body.urgency),
        ).fetchone()["id"]
        cb = dict(conn.execute("SELECT * FROM callback_requests WHERE id = ?",
                               (new_id,)).fetchone())
    background.add_task(notify.staff_callback_alert, cb)
    return cb


@app.get("/callbacks")
def list_callbacks(status: Literal["open", "done", "all"] = "open"):
    """Staff view: who needs a call back. Urgent first, then oldest first."""
    q = "SELECT * FROM callback_requests"
    params: list = []
    if status != "all":
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY urgency = 'urgent' DESC, created_at"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q, params)]


@app.post("/callbacks/{cb_id}/done")
def complete_callback(cb_id: int):
    with db.connect() as conn:
        cur = conn.execute("UPDATE callback_requests SET status = 'done' WHERE id = ?", (cb_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Callback request not found")
        return dict(conn.execute("SELECT * FROM callback_requests WHERE id = ?",
                                 (cb_id,)).fetchone())


# ---------- Phase 2: tokens for the website's "Talk to Shanaya" button ----------
# The browser needs an access token to join a LiveKit room. Tokens are signed with the
# LiveKit API secret, which must never reach a browser, so the backend signs a token for
# ONE new room and hands only that token out. Maya joins new rooms automatically.
#
# This endpoint is public (the website calls it without our API key), so anyone could use
# it to start calls that spend provider credits. Protections:
#   - each token works for one fresh room only, and expires after a few minutes
#   - a per-IP rate limit (in memory: resets on restart, per server instance; good enough
#     for a demo, a production system would use something like Redis)
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()
TOKEN_TTL = timedelta(minutes=10)
TOKEN_LIMIT = int(os.getenv("TOKEN_RATE_LIMIT", "5"))  # tokens per IP per window
TOKEN_WINDOW_S = 10 * 60
_token_requests: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Render sits behind a proxy, so the real visitor IP is in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/token")
def create_token(request: Request):
    if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
        raise HTTPException(503, "Voice calls aren't set up on this server yet")

    ip, now = _client_ip(request), monotonic()
    recent = _token_requests[ip]
    while recent and now - recent[0] > TOKEN_WINDOW_S:
        recent.popleft()
    if len(recent) >= TOKEN_LIMIT:
        raise HTTPException(429, "Too many call attempts. Please wait a few minutes and try again.")
    recent.append(now)

    room = f"web-{uuid.uuid4().hex[:12]}"
    identity = f"visitor-{uuid.uuid4().hex[:8]}"
    token = (
        livekit_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name("Website visitor")
        .with_ttl(TOKEN_TTL)
        .with_grants(livekit_api.VideoGrants(
            room_join=True,
            room=room,              # this one room only
            can_publish=True,       # send microphone audio
            can_subscribe=True,     # hear Maya
            can_publish_data=False, # the website only listens for calendar updates
        ))
        .to_jwt()
    )
    log.info("issued call token for room %s", room)
    return {
        "server_url": LIVEKIT_URL,
        "participant_token": token,
        "room_name": room,
        "participant_identity": identity,
    }