"use client";

import { useEffect, useRef, useState } from "react";
import {
  ParticipantKind,
  Room,
  RoomEvent,
  Track,
  type Participant,
  type RemoteTrack,
} from "livekit-client";
import { API_URL } from "@/lib/api";
import { CALENDAR_UPDATED_EVENT } from "./ChairDay";

type CallState = "idle" | "connecting" | "waiting" | "live" | "ended" | "error";
type AgentState = "initializing" | "idle" | "listening" | "thinking" | "speaking";

// The agent publishes what it's doing in this participant attribute (LiveKit Agents standard).
const AGENT_STATE_ATTRIBUTE = "lk.agent.state";
const AGENT_STATUS: Record<AgentState, string> = {
  initializing: "Shanaya is getting ready…",
  idle: "Shanaya is here",
  listening: "Shanaya is listening",
  thinking: "Shanaya is thinking…",
  speaking: "Shanaya is speaking",
};

const isAgent = (p: Participant) => p.kind === ParticipantKind.AGENT;

function isMicrophoneError(e: unknown): boolean {
  const name = e instanceof Error ? e.name : "";
  return name === "NotAllowedError" || name === "NotFoundError" || name === "NotReadableError";
}

/**
 * The "Talk to Shanaya" button.
 * 1. Asks our backend for a short-lived token (the LiveKit secret never reaches the browser).
 * 2. Joins a new LiveKit room over WebRTC with the microphone on; Shanaya joins automatically.
 * 3. Plays her audio, shows what she's doing, and refreshes the chair timeline the moment she
 *    books, moves or cancels something (she sends a "calendar_updated" message into the room).
 */
export default function TalkToShanaya() {
  const [call, setCall] = useState<CallState>("idle");
  const [agentState, setAgentState] = useState<AgentState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [level, setLevel] = useState(0); // Shanaya's voice level, 0..1, drives the ring
  const roomRef = useRef<Room | null>(null);
  const audioRef = useRef<HTMLDivElement>(null);

  // Leave the room if the visitor navigates away mid-call.
  useEffect(() => () => void roomRef.current?.disconnect(), []);

  // While live, sample Shanaya's audio level ten times a second for the speaking ring.
  useEffect(() => {
    if (call !== "live") return;
    const timer = setInterval(() => {
      const agent = [...(roomRef.current?.remoteParticipants.values() ?? [])].find(isAgent);
      setLevel(agent?.audioLevel ?? 0);
    }, 100);
    return () => clearInterval(timer);
  }, [call]);

  async function start() {
    setError(null);
    setAgentState(null);
    setCall("connecting");

    // 1. Token from our backend
    let info: { server_url: string; participant_token: string };
    try {
      const res = await fetch(`${API_URL}/token`, { method: "POST" });
      if (!res.ok) {
        const detail = (await res.json().catch(() => null))?.detail;
        throw new Error(detail ?? `The call service answered with error ${res.status}.`);
      }
      info = await res.json();
    } catch (e) {
      setCall("error");
      setError(
        e instanceof Error && e.message !== "Failed to fetch"
          ? e.message
          : "Couldn't reach the booking system. Check your connection and try again.",
      );
      return;
    }

    // 2. Join the room
    const room = new Room({ adaptiveStream: true, dynacast: true });
    roomRef.current = room;

    room
      .on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind === Track.Kind.Audio) audioRef.current?.appendChild(track.attach());
      })
      .on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
        track.detach().forEach((el) => el.remove());
      })
      .on(RoomEvent.ParticipantConnected, (p) => {
        if (isAgent(p)) setCall("live");
      })
      .on(RoomEvent.ParticipantAttributesChanged, (changed, p) => {
        const state = changed[AGENT_STATE_ATTRIBUTE];
        if (state && isAgent(p)) {
          setAgentState(state as AgentState);
          setCall("live");
        }
      })
      .on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
        // 3. Live calendar: Shanaya announces bookings on the "calendar" topic
        if (topic !== "calendar") return;
        try {
          const msg = JSON.parse(new TextDecoder().decode(payload));
          if (msg.type === "calendar_updated") {
            window.dispatchEvent(new CustomEvent(CALENDAR_UPDATED_EVENT, { detail: { day: msg.day } }));
          }
        } catch {
          // ignore messages that aren't ours
        }
      })
      .on(RoomEvent.Disconnected, () => {
        // Covers the visitor hanging up AND Shanaya ending the call herself.
        setCall((c) => (c === "error" ? c : "ended"));
        setAgentState(null);
        setLevel(0);
        roomRef.current = null;
      });

    try {
      await room.connect(info.server_url, info.participant_token);
      await room.startAudio(); // allowed because this runs right after the visitor's click
      await room.localParticipant.setMicrophoneEnabled(true);
      // Shanaya may already be in the room, or may still be waking up (cold start).
      setCall([...room.remoteParticipants.values()].some(isAgent) ? "live" : "waiting");
    } catch (e) {
      setCall("error");
      setError(
        isMicrophoneError(e)
          ? "Your browser blocked the microphone. Allow microphone access for this site, then try again."
          : "The call couldn't connect. Please try again in a moment.",
      );
      await room.disconnect();
    }
  }

  function hangUp() {
    void roomRef.current?.disconnect();
  }

  const busy = call === "connecting" || call === "waiting";
  const live = call === "live";

  let status = "Uses your microphone. Shanaya will tell you before anything is recorded.";
  if (call === "connecting") status = "Connecting…";
  if (call === "waiting") status = "Shanaya usually joins within a few seconds.";
  if (live) status = agentState ? AGENT_STATUS[agentState] : "Shanaya is joining…";
  if (call === "ended") status = "Call ended. Talk to Shanaya again any time.";
  if (call === "error") status = "The call didn't start.";

  return (
    <div className="flex flex-col items-center gap-5 text-center">
      <div className="relative grid place-items-center">
        {/* the ring grows with Shanaya's voice while she speaks */}
        <span
          aria-hidden
          className="absolute size-44 rounded-full bg-teal/25 transition-transform duration-100"
          style={{ transform: `scale(${live ? 1 + Math.min(level * 3, 1) * 0.35 : 1})` }}
        />
        {live ? (
          <div className="relative grid size-44 place-items-center rounded-full bg-teal text-white">
            <MicIcon />
          </div>
        ) : (
          <button
            type="button"
            onClick={start}
            disabled={busy}
            className="relative grid size-44 place-items-center rounded-full bg-teal px-6 font-display text-xl font-semibold text-white shadow-sm transition-colors hover:bg-teal-dark disabled:cursor-wait disabled:opacity-80"
          >
            <span className="flex flex-col items-center gap-2">
              <MicIcon />
              {busy ? "Connecting…" : call === "ended" || call === "error" ? "Call again" : "Talk to Shanaya"}
            </span>
          </button>
        )}
      </div>

      <p aria-live="polite" className={`max-w-[28ch] ${live ? "text-lg font-semibold" : "text-muted"}`}>
        {status}
      </p>

      {error && (
        <p role="alert" className="max-w-[34ch] rounded-lg bg-white px-4 py-3 text-sm text-ink ring-2 ring-rose">
          {error}
        </p>
      )}

      {(live || call === "waiting") && (
        <button
          type="button"
          onClick={hangUp}
          className="rounded-full border-2 border-ink px-5 py-2 font-semibold hover:bg-ink hover:text-mist"
        >
          End call
        </button>
      )}

      {/* Shanaya's voice plays through audio elements added here */}
      <div ref={audioRef} hidden />
    </div>
  );
}

function MicIcon() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="size-8"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}