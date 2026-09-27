"""Per-call metrics (Step 5): how fast Maya responds, where the time goes, and what a call costs.

Every turn logs two lines:
  RESPONSE 1.24s  (you stopped talking -> Maya started speaking)
  breakdown: end-of-turn 0.61s (stt 0.28s) | LLM first token 0.45s | TTS first audio 0.18s

At the end of each call, a summary is logged and saved to agent/call_logs/<time>.json,
along with the transcript if the caller didn't decline recording.
"""
import json
import logging
import statistics
import time
from datetime import datetime
from pathlib import Path

from livekit.agents import (
    AgentStateChangedEvent,
    MetricsCollectedEvent,
    UserStateChangedEvent,
    metrics as lk_metrics,
)

logger = logging.getLogger("booking-agent")

CALL_LOG_DIR = Path(__file__).parent / "call_logs"
SLOW_RESPONSE_S = 2.0  # responses slower than this get flagged

# ROUGH price placeholders in USD. They change often, so check each provider's
# pricing page and edit these. Unknown models are counted as $0 and flagged.
PRICES = {
    "llm_per_1m_tokens": {  # (input, output)
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4o-mini": (0.15, 0.60),
        # free tiers (within their limits)
        "gemini-2.5-flash": (0.0, 0.0),
        "llama-3.3-70b-versatile": (0.0, 0.0),
    },
    "stt_per_minute": {
        "nova-3": 0.0077,
        "gpt-4o-mini-transcribe": 0.003,
    },
    "tts_per_1k_chars": {
        "sonic-3": 0.04,
        "gpt-4o-mini-tts": 0.017,
    },
}


class CallMetrics:
    def __init__(self) -> None:
        self.started_at = datetime.now()
        self._t0 = time.perf_counter()
        self.usage = lk_metrics.ModelUsageCollector()
        self.turns: dict[str, dict] = {}   # speech_id -> timing breakdown
        self.latencies: list[float] = []   # measured response times, in seconds
        self._user_stopped_at: float | None = None

    # ---------- event handlers (wired up in agent.py) ----------

    def on_metrics(self, ev: MetricsCollectedEvent) -> None:
        m = ev.metrics
        self.usage.collect(m)
        sid = getattr(m, "speech_id", None)
        if not sid:
            return
        turn = self.turns.setdefault(sid, {})
        if isinstance(m, lk_metrics.EOUMetrics):
            turn["eou"] = m.end_of_utterance_delay
            turn["stt"] = m.transcription_delay
        elif isinstance(m, lk_metrics.LLMMetrics) and not m.cancelled:
            turn.setdefault("llm_ttft", m.ttft)
        elif isinstance(m, lk_metrics.TTSMetrics) and not m.cancelled:
            turn.setdefault("tts_ttfb", m.ttfb)
            if not turn.get("logged"):
                turn["logged"] = True
                self._log_breakdown(turn)

    def on_user_state(self, ev: UserStateChangedEvent) -> None:
        if ev.old_state == "speaking" and ev.new_state == "listening":
            self._user_stopped_at = time.perf_counter()

    def on_agent_state(self, ev: AgentStateChangedEvent) -> None:
        # The number that matters to callers: silence between their last word and Maya's first.
        if ev.new_state == "speaking" and self._user_stopped_at is not None:
            latency = time.perf_counter() - self._user_stopped_at
            self._user_stopped_at = None
            self.latencies.append(latency)
            flag = "  <-- SLOW" if latency > SLOW_RESPONSE_S else ""
            logger.info("RESPONSE %.2fs (you stopped talking -> Maya started)%s", latency, flag)

    @staticmethod
    def _log_breakdown(turn: dict) -> None:
        parts = []
        if "eou" in turn:
            parts.append(f"end-of-turn {turn['eou']:.2f}s (stt {turn['stt']:.2f}s)")
        if "llm_ttft" in turn:
            parts.append(f"LLM first token {turn['llm_ttft']:.2f}s")
        if "tts_ttfb" in turn:
            parts.append(f"TTS first audio {turn['tts_ttfb']:.2f}s")
        logger.info("   breakdown: %s", " | ".join(parts))

    # ---------- end of call ----------

    def _estimate_cost(self) -> tuple[list[dict], float]:
        rows, total = [], 0.0
        for u in self.usage.flatten():
            # keep "type", drop empty counters so the log stays readable
            d = {k: v for k, v in u.model_dump().items() if k == "type" or v not in (0, 0.0, None, "")}
            model, cost, known = d.get("model", "?"), 0.0, True
            if d.get("type") == "llm_usage":
                price = PRICES["llm_per_1m_tokens"].get(model)
                if price:
                    cost = (d.get("input_tokens", 0) * price[0]
                            + d.get("output_tokens", 0) * price[1]) / 1_000_000
                else:
                    known = False
            elif d.get("type") == "stt_usage":
                price = PRICES["stt_per_minute"].get(model)
                if price:
                    cost = d.get("audio_duration", 0) / 60 * price
                else:
                    known = False
            elif d.get("type") == "tts_usage":
                price = PRICES["tts_per_1k_chars"].get(model)
                if price:
                    cost = d.get("characters_count", 0) / 1000 * price
                else:
                    known = False
            else:
                continue  # turn-detector / interruption usage: included in LiveKit plan
            total += cost
            rows.append({**d, "est_cost_usd": round(cost, 5), "price_known": known})
        return rows, total

    def summary(self) -> dict:
        lat = sorted(self.latencies)
        usage, cost = self._estimate_cost()
        return {
            "duration_s": round(time.perf_counter() - self._t0, 1),
            "turns": len(lat),
            "response_latency_s": {
                "avg": round(statistics.mean(lat), 2) if lat else None,
                "p50": round(statistics.median(lat), 2) if lat else None,
                "p90": round(lat[int(0.9 * (len(lat) - 1))], 2) if lat else None,
                "max": round(lat[-1], 2) if lat else None,
                "slow_turns": sum(1 for x in lat if x > SLOW_RESPONSE_S),
            },
            "usage": usage,
            "est_cost_usd": round(cost, 4),
        }

    def finish(self, transcript: dict | None, consent: bool | None) -> None:
        """Log the summary and save a call log. Transcript is withheld if consent was declined."""
        s = self.summary()
        r = s["response_latency_s"]
        if s["turns"]:
            latency = (f"response avg {r['avg']}s, p90 {r['p90']}s, max {r['max']}s, "
                       f"{r['slow_turns']} slow")
        else:
            latency = "no completed turns (did the agent ever speak?)"
        logger.info("CALL SUMMARY: %ss, %d turns | %s | est. cost $%.4f",
                    s["duration_s"], s["turns"], latency, s["est_cost_usd"])
        record = {"started_at": self.started_at.isoformat(timespec="seconds"), "metrics": s}
        if consent is False:
            record["transcript"] = None
            record["transcript_note"] = "caller declined recording"
        else:
            record["transcript"] = transcript
        try:
            CALL_LOG_DIR.mkdir(exist_ok=True)
            path = CALL_LOG_DIR / f"{self.started_at:%Y-%m-%d_%H-%M-%S}.json"
            path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
            logger.info("call log saved: %s", path)
        except OSError:
            logger.exception("could not save call log")