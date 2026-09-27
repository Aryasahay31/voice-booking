"""Fast, free tests for the confirmation gate's read-back check (no LLM involved).

Written from a real call: Maya's read-back was split in two by the caller saying
"Thanks." mid-sentence, and the gate made her confirm a second time.
Run: python -m pytest tests/test_gate.py
"""
from agent import mentions_time, readback_covers

PHONE = "6202764686"


# ---------- time matching ----------

def test_time_formats_are_recognised():
    for text in ("at 4:00 PM", "at 4 PM", "at 4pm", "at four PM", "at 16:00", "four o'clock"):
        assert mentions_time(text, "16:00"), text


def test_half_hours_are_recognised():
    for text in ("at 4:30 PM", "at half past four", "at four thirty"):
        assert mentions_time(text, "16:30"), text


def test_wrong_time_is_rejected():
    assert not mentions_time("at 5 PM", "16:00")
    assert not mentions_time("at 4:30 PM", "16:00")
    assert not mentions_time("at 14:00", "16:00")


# ---------- whole read-back ----------

def test_split_readback_from_the_real_call_counts():
    """The exact messages from the call where the caller was asked twice."""
    last_two = [
        "Thanks, Ritu! Just to",
        "confirm, you'd like to book a haircut for this Wednesday, September 30th, "
        "at 4:00 PM. Is that right? And your phone number is 6202764686.",
    ]
    assert readback_covers(" ".join(last_two), PHONE, "16:00")


def test_readback_with_spoken_digits_counts():
    text = "Haircut at four PM, number six two zero two seven six four six eight six. Correct?"
    assert readback_covers(text, PHONE, "16:00")


def test_readback_with_a_wrong_digit_does_not_count():
    text = "Haircut at 4 PM, number 6202764685. Correct?"
    assert not readback_covers(text, PHONE, "16:00")


def test_readback_without_the_number_does_not_count():
    assert not readback_covers("Shall I book the haircut at 4 PM?", PHONE, "16:00")


def test_readback_of_a_different_time_does_not_count():
    """Caller heard '4 PM' read back, then said 'make it 5': 5 PM needs its own read-back."""
    text = "Haircut at 4 PM, number 6202764686. Correct?"
    assert not readback_covers(text, PHONE, "17:00")