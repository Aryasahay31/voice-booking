"""Which AI providers the agent uses, with automatic fallbacks.

Providers are added only if you have a key for them, in this order:

  Stage | 1st                         | 2nd                          | 3rd                  | last
  ------+-----------------------------+------------------------------+----------------------+-------------------
  STT   | Deepgram (DEEPGRAM_API_KEY) | deepgram/nova-3 via LiveKit  | OpenAI (if key)      |
  LLM   | gemini via LiveKit          | Groq GPT-OSS 120B            | Gemini direct        | OpenAI (if key)
        | (LiveKit credits)           | (GROQ_API_KEY, free tier)    | (opt-in, see below)  |
  TTS   | Cartesia (CARTESIA_API_KEY) | cartesia/sonic-3 via LiveKit | OpenAI (if key)      |

"via LiveKit" = LiveKit Inference, billed to your LiveKit Cloud account (uses the
LIVEKIT_API_KEY / SECRET you already have). Spreading backups across different
companies AND accounts means one outage or empty balance can't take everything down.

Free tiers have rate limits (fine for testing, not for real traffic), and some free
tiers may use your requests to improve their models; check each provider's terms.

Testing: set SIMULATE_OUTAGE in agent/.env to break the FIRST provider of a stage:
  SIMULATE_OUTAGE=stt | llm | tts | all      (leave empty for normal use)
"""
import logging
import os

from livekit.agents import inference, llm, stt, tts, vad
from livekit.plugins import cartesia, deepgram, openai

# LiveKit plugins must be imported on the main thread (they register themselves on import),
# so import here at module level, never inside the build_* functions, which run in a thread.
try:
    from livekit.plugins import google
except ImportError:  # livekit-agents[google] not installed
    google = None

logger = logging.getLogger("booking-agent")

CARTESIA_MODEL = os.getenv("CARTESIA_MODEL", "sonic-3")
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID")  # optional: pick one at play.cartesia.ai
SIMULATE_OUTAGE = os.getenv("SIMULATE_OUTAGE", "").lower()

# Google retired gemini-2.5-flash for new API keys; its error message recommends this one.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Groq retired llama-3.3-70b-versatile on 2026-08-16; this is their recommended replacement.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

_FAKE_KEY = {"api_key": "simulated-outage-invalid-key"}


class GroqLLM(openai.LLM):
    """Groq via its OpenAI-compatible API, with one fix.

    Gemini attaches hidden metadata ("extra") to messages it writes. In OpenAI format that
    becomes an "extra_content" field, which Groq rejects with a 400 error. So if Gemini has
    answered earlier in the call, Groq would fail on every turn after. We send Groq a copy
    of the history with that metadata removed (the real history is left untouched, since
    Gemini needs it)."""

    def chat(self, *, chat_ctx: llm.ChatContext, **kwargs):
        cleaned = [
            item.model_copy(update={"extra": {}}) if getattr(item, "extra", None) else item
            for item in chat_ctx.items
        ]
        return super().chat(chat_ctx=llm.ChatContext(items=cleaned), **kwargs)


def _has(env_var: str) -> bool:
    return bool(os.getenv(env_var, "").strip())


def _broken(stage: str) -> dict:
    """Return a fake API key for the first provider of this stage if simulating an outage."""
    if SIMULATE_OUTAGE in (stage, "all"):
        logger.warning("SIMULATING OUTAGE: first %s provider is using an invalid key", stage.upper())
        return _FAKE_KEY
    return {}


def _provider_name(p) -> str:
    """Short readable name like 'groq:llama-3.3-70b-versatile' or 'inference:google/...'."""
    if isinstance(p, GroqLLM):
        vendor = "groq"
    else:
        parts = type(p).__module__.split(".")
        vendor = parts[-2] if len(parts) >= 2 else type(p).__name__.lower()
    return f"{vendor}:{getattr(p, 'model', '?')}"


def _describe(stage: str, providers: list) -> None:
    names = [_provider_name(p) for p in providers]
    logger.info("%s chain: %s", stage, " -> ".join(names))


def build_stt(vad_model: vad.VAD) -> stt.STT:
    chain: list[stt.STT] = []
    if _has("DEEPGRAM_API_KEY"):
        chain.append(deepgram.STT(model="nova-3", **_broken("stt")))
    chain.append(inference.STT("deepgram/nova-3", language="en"))
    if _has("OPENAI_API_KEY"):
        chain.append(openai.STT(model="gpt-4o-mini-transcribe"))
    _describe("STT", chain)
    # vad lets the adapter handle non-streaming backups (it cuts audio into utterances).
    return stt.FallbackAdapter(chain, vad=vad_model)


def build_llm() -> llm.LLM:
    """Order chosen from real call logs:

    1. Gemini via LiveKit Inference: the most reliable in testing (never rate-limited us).
    2. Groq: fast, but the free tier allows only ~8,000 tokens/minute. One voice turn with
       our prompt + tools + history is ~3,000-5,000 tokens, so it runs out after 2 turns.
    3. Gemini direct (only if ENABLE_GEMINI_DIRECT=true): the free tier allows ~20 requests
       per DAY, and Gemini 3 rejects tool calls in the history written by other models
       (missing "thought_signature"), so it's off by default.
    4. OpenAI (if OPENAI_API_KEY is set and has credit).

    Order matters beyond speed: while a provider is marked down, the FallbackAdapter probes
    it with a full request on every turn. A flaky provider at the front burns its own quota
    and adds delay; the reliable one should lead.
    """
    chain: list[llm.LLM] = []
    chain.append(inference.LLM("google/gemini-2.5-flash", **_broken("llm")))
    if _has("GROQ_API_KEY"):
        chain.append(GroqLLM(
            model=GROQ_MODEL,
            base_url="https://api.groq.com/openai/v1",
            reasoning_effort="low",  # gpt-oss "thinks" before answering; keep it short for voice
            api_key=os.environ["GROQ_API_KEY"],
        ))
    if os.getenv("ENABLE_GEMINI_DIRECT", "").lower() == "true":
        if google is None:
            logger.warning("ENABLE_GEMINI_DIRECT is set but the Google plugin isn't installed")
        elif _has("GOOGLE_API_KEY"):
            chain.append(google.LLM(model=GEMINI_MODEL))
    if _has("OPENAI_API_KEY"):
        chain.append(openai.LLM(model="gpt-4.1-mini"))
    _describe("LLM", chain)
    return llm.FallbackAdapter(
        chain,
        # Google rejects request deadlines under 10s, so 10s is the lowest that works for all.
        attempt_timeout=10.0,
    )


def build_tts() -> tts.TTS:
    voice = {"voice": CARTESIA_VOICE_ID} if CARTESIA_VOICE_ID else {}
    chain: list[tts.TTS] = []
    if _has("CARTESIA_API_KEY"):
        chain.append(cartesia.TTS(model=CARTESIA_MODEL, **voice, **_broken("tts")))
    chain.append(inference.TTS(f"cartesia/{CARTESIA_MODEL}", **voice))  # same voice, LiveKit billing
    if _has("OPENAI_API_KEY"):
        # Different voice if this one kicks in; the instructions keep the tone similar.
        chain.append(openai.TTS(
            model="gpt-4o-mini-tts",
            voice="coral",
            instructions="Warm, friendly, upbeat salon receptionist. Natural pace.",
        ))
    _describe("TTS", chain)
    return tts.FallbackAdapter(chain)


def watch_availability(stt_model: stt.STT, llm_model: llm.LLM, tts_model: tts.TTS) -> None:
    """Log whenever a provider goes down or comes back, so switches are visible."""

    def make_handler(stage: str):
        def handler(ev) -> None:
            name = _provider_name(getattr(ev, stage.lower()))
            if ev.available:
                logger.info("%s provider RECOVERED: %s", stage, name)
            else:
                logger.warning("%s provider DOWN, switching to backup: %s", stage, name)

        return handler

    stt_model.on("stt_availability_changed", make_handler("STT"))
    llm_model.on("llm_availability_changed", make_handler("LLM"))
    tts_model.on("tts_availability_changed", make_handler("TTS"))