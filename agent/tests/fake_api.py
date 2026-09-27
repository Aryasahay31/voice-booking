"""A fake booking API for evals.

Tests shouldn't depend on your real backend or database, so the agent's HTTP client is
pointed at this in-memory fake instead. It records every request, so tests can check
things like "no booking was made before the caller confirmed".
"""
import json
from datetime import date, datetime, timedelta

import httpx

SERVICES = [
    {"id": 1, "name": "Haircut", "duration_min": 30, "price": 500},
    {"id": 2, "name": "Hair Colour", "duration_min": 90, "price": 2500},
    {"id": 3, "name": "Beard Trim", "duration_min": 15, "price": 200},
    {"id": 4, "name": "Facial", "duration_min": 60, "price": 1200},
    {"id": 5, "name": "Manicure", "duration_min": 45, "price": 700},
]
BY_ID = {s["id"]: s for s in SERVICES}
ALL_SLOTS = [f"{h:02d}:{m:02d}" for h in range(10, 19) for m in (0, 15, 30, 45)]


class FakeBookingAPI:
    def __init__(self, down: bool = False) -> None:
        self.down = down                      # True = every request fails (backend outage)
        self.calls: list[tuple[str, str, dict]] = []  # (method, path, params/body)
        self.appointments: list[dict] = []
        self.callbacks: list[dict] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://fake-api", transport=httpx.MockTransport(self._handle))

    def made(self, method: str, path_prefix: str) -> list[dict]:
        """All recorded requests matching a method and path prefix."""
        return [data for m, p, data in self.calls if m == method and p.startswith(path_prefix)]

    def add_booking(self, service_id: int, name: str, phone: str, start: str) -> None:
        """Pre-seed a booking (e.g. another customer's) before a test."""
        self._create(service_id, name, phone, start)

    # ---------- internals ----------

    def _create(self, service_id: int, name: str, phone: str, start: str) -> dict:
        svc = BY_ID[service_id]
        start_dt = datetime.fromisoformat(start)
        appt = {
            "id": len(self.appointments) + 1, "service_id": service_id, "service": svc["name"],
            "customer_name": name, "phone": "".join(c for c in phone if c.isdigit()),
            "start_time": start_dt.isoformat(timespec="minutes"),
            "end_time": (start_dt + timedelta(minutes=svc["duration_min"])).isoformat(timespec="minutes"),
            "status": "booked", "price": svc["price"], "confirmation_sent": False,
        }
        self.appointments.append(appt)
        return appt

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("booking API down (simulated)", request=request)

        path, method, params = request.url.path, request.method, request.url.params
        body = json.loads(request.content) if request.content else {}
        recorded = {k: (v if len(v := params.get_list(k)) > 1 else v[0]) for k in params.keys()}
        self.calls.append((method, path, body or recorded))

        if method == "GET" and path == "/services":
            return httpx.Response(200, json=SERVICES)

        if method == "GET" and path == "/availability":
            svc = BY_ID.get(int(params["service_id"]))
            day = date.fromisoformat(params["day"])
            if not svc:
                return httpx.Response(404, json={"detail": "Service not found"})
            closed = day.weekday() == 6
            return httpx.Response(200, json={
                "day": day.isoformat(), "service": svc["name"], "closed": closed,
                "slots": [] if closed else ALL_SLOTS,
            })

        if method == "GET" and path == "/availability/back-to-back":
            ids = [int(i) for i in params.get_list("service_ids")]
            day = date.fromisoformat(params["day"])
            names = [BY_ID[i]["name"] for i in ids]
            if day.weekday() == 6:
                return httpx.Response(200, json={"day": day.isoformat(), "services": names,
                                                 "closed": True, "options": []})
            options = []
            for first in ("10:00", "11:00", "14:00"):
                t, plan = datetime.strptime(first, "%H:%M"), []
                for i in ids:
                    plan.append({"service": BY_ID[i]["name"], "start": t.strftime("%H:%M")})
                    t += timedelta(minutes=BY_ID[i]["duration_min"])
                options.append({"plan": plan, "all_done_by": t.strftime("%H:%M")})
            return httpx.Response(200, json={"day": day.isoformat(), "services": names,
                                             "closed": False, "options": options})

        if method == "POST" and path == "/appointments":
            if body.get("service_id") not in BY_ID:
                return httpx.Response(404, json={"detail": "Service not found"})
            appt = self._create(body["service_id"], body["customer_name"], body["phone"], body["start_time"])
            return httpx.Response(201, json=appt)

        if method == "GET" and path == "/appointments":
            digits = "".join(c for c in params["phone"] if c.isdigit())
            found = [a for a in self.appointments
                     if a["status"] == "booked" and a["phone"][-10:] == digits[-10:]]
            return httpx.Response(200, json=found)

        if path.startswith("/appointments/"):
            appt_id = int(path.split("/")[2])
            appt = next((a for a in self.appointments if a["id"] == appt_id), None)
            if not appt:
                return httpx.Response(404, json={"detail": "Appointment not found"})
            if method == "PATCH":
                start = datetime.fromisoformat(body["start_time"])
                appt["start_time"] = start.isoformat(timespec="minutes")
                return httpx.Response(200, json=appt)
            if method == "POST" and path.endswith("/cancel"):
                appt["status"] = "cancelled"
                return httpx.Response(200, json=appt)

        if method == "POST" and path == "/callbacks":
            cb = {"id": len(self.callbacks) + 1, **body, "status": "open"}
            self.callbacks.append(cb)
            return httpx.Response(201, json=cb)

        return httpx.Response(404, json={"detail": f"No fake route for {method} {path}"})