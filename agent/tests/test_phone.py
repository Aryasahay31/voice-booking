"""Phase 4 (phone calls): fast, free tests, no LLM needed.
Run: python -m pytest tests/test_phone.py
"""
from types import SimpleNamespace

from livekit import rtc
from livekit.plugins import noise_cancellation

from agent import caller_number, pick_noise_cancellation, readback_covers_caller_id

SIP = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
STANDARD = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD


def model(options) -> str:
    return options.options["modelPath"]


# ---------- noise cancellation ----------

def test_phone_callers_get_the_telephony_model():
    params = SimpleNamespace(participant=SimpleNamespace(kind=SIP), track=None)
    assert model(pick_noise_cancellation(params)) == model(noise_cancellation.BVCTelephony())


def test_website_visitors_get_the_standard_model():
    params = SimpleNamespace(participant=SimpleNamespace(kind=STANDARD), track=None)
    assert model(pick_noise_cancellation(params)) == model(noise_cancellation.BVC())


# ---------- caller ID ----------

def room_with(*participants):
    return SimpleNamespace(remote_participants={str(i): p for i, p in enumerate(participants)})


def test_reads_the_callers_number_from_caller_id():
    caller = SimpleNamespace(kind=SIP, attributes={"sip.phoneNumber": "+916202764686"})
    assert caller_number(room_with(caller)) == "916202764686"


def test_website_calls_have_no_caller_number():
    visitor = SimpleNamespace(kind=STANDARD, attributes={})
    assert caller_number(room_with(visitor)) is None


def test_invalid_caller_numbers_are_ignored():
    # e.g. a softphone presenting a short internal ID instead of a phone number
    caller = SimpleNamespace(kind=SIP, attributes={"sip.phoneNumber": "1001"})
    assert caller_number(room_with(caller)) is None


def test_console_mode_does_not_crash():
    assert caller_number(SimpleNamespace()) is None  # no remote_participants at all


# ---------- read-back for the caller-ID number ----------

def test_last_four_digits_and_time_are_enough_for_caller_id():
    text = "Haircut at 4 PM, and I'll use the number you're calling from, ending in 4686. Correct?"
    assert readback_covers_caller_id(text, "916202764686", "16:00")


def test_wrong_last_four_digits_do_not_count():
    text = "Haircut at 4 PM, on the number ending in 4685. Correct?"
    assert not readback_covers_caller_id(text, "916202764686", "16:00")


def test_time_is_still_required():
    text = "I'll use the number ending in 4686. Shall I book it?"
    assert not readback_covers_caller_id(text, "916202764686", "16:00")