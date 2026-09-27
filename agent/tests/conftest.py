"""Shared setup for the evals: which LLM runs Maya, which LLM judges her, and a helper
that starts a text-only session (no audio, no LiveKit room) against the fake API."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so "import agent" works

import agent as maya_agent  # noqa: E402  (also loads agent/.env)
from livekit.agents import AgentSession, inference, llm  # noqa: E402

from tests.fake_api import FakeBookingAPI  # noqa: E402

# One reliable model for both roles. Uses your LiveKit Cloud credits (LIVEKIT_API_KEY/SECRET).
# Each full run makes roughly 40-60 LLM requests, so keep an eye on usage.
AGENT_MODEL = os.getenv("EVAL_AGENT_LLM", "google/gemini-2.5-flash")
JUDGE_MODEL = os.getenv("EVAL_JUDGE_LLM", "google/gemini-2.5-flash")
TZ = ZoneInfo(os.getenv("BUSINESS_TZ", "UTC"))


def _today() -> date:
    return datetime.now(TZ).date()


def next_weekday(weekday: int) -> date:
    """Next date (after today) with this weekday; 0 = Monday ... 6 = Sunday."""
    d = _today() + timedelta(days=1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def next_open_day() -> date:
    d = _today() + timedelta(days=1)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def spoken(d: date) -> str:
    """How a caller would say it: 'Saturday 3 October'."""
    return f"{d:%A} {d.day} {d:%B}"


def tool_calls(result, name: str | None = None) -> list[dict]:
    """Arguments of every tool call Maya made in this turn (optionally only one tool)."""
    out = []
    for ev in result.events:
        if ev.type == "function_call" and (name is None or ev.item.name == name):
            out.append(json.loads(ev.item.arguments or "{}"))
    return out


def digits(s: str) -> str:
    return "".join(c for c in str(s) if c.isdigit())


@pytest.fixture
def api() -> FakeBookingAPI:
    return FakeBookingAPI()


@pytest.fixture
async def judge():
    async with inference.LLM(JUDGE_MODEL) as judge_llm:
        yield judge_llm


@asynccontextmanager
async def start_maya(api: FakeBookingAPI, agent_llm: llm.LLM | None = None):
    """Text-only session with Maya using the local booking tools against the fake API."""
    async with (
        (agent_llm or inference.LLM(AGENT_MODEL)) as the_llm,
        AgentSession(llm=the_llm) as session,
    ):
        await session.start(maya_agent.BookingAssistant(api.client()))
        yield session