"use client";

import { useCallback, useEffect, useRef, useSyncExternalStore, type KeyboardEvent, type PointerEvent } from "react";
import { Mic, MicOff, PhoneOff } from "lucide-react";
import { Track, type Participant } from "livekit-client";
import {
  RoomAudioRenderer, RoomContext, useIsSpeaking, useMultibandTrackVolume, useParticipantAttributes, useParticipants, useTracks,
} from "@livekit/components-react";
import { Identity } from "@/components/teams/shared/components/Identity";
import { agentTint } from "@/lib/agentTint";
import { AudioVisualizerRadial } from "@/components/voice/AudioVisualizerRadial";
import { errorText } from "@/components/voice/voiceErrors";
import { CEO_IDENTITY, useHuddle, type HuddleError } from "./HuddleProvider";

const BARS = 24;
const RADIUS = 36; // the orb's core is 64px; Lightbulb's tile is bigger
const TAP_MS = 250;
const LIGHTBULB = "Lightbulb";
const REDUCED = "(prefers-reduced-motion: reduce)";
const subscribeReduced = (onChange: () => void) => {
  const mql = window.matchMedia(REDUCED);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
};
const reducedSnapshot = () => window.matchMedia(REDUCED).matches;

/** The Lightbulb tile's status, from the worker's participant attributes (huddle_agent.huddle `_state`). */
export function huddleStatus(attrs: Readonly<Record<string, string>> | undefined): string {
  const speaker = attrs?.["lightbulb.speaker"];
  const working = attrs?.["lightbulb.working"] ?? "";
  switch (attrs?.["lightbulb.status"]) {
    case "listening": return speaker ? `Listening to ${speaker}` : "Listening";
    case "thinking": return "Thinking…";
    case "speaking": return "Speaking";
    case "working": {
      const [who, what] = working.split(" — ");
      return what ? `${who} is working: ${what}` : who ? `${who} is working` : "Thinking…";
    }
    default: return "Following along";
  }
}

function Status({ participant, speaking }: { participant: Participant; speaking: boolean }) {
  const { attributes } = useParticipantAttributes({ participant });
  return <span className="huddle-status" aria-live="polite">{speaking ? "Speaking" : huddleStatus(attributes)}</span>;
}

export function huddleErrorText(e: HuddleError): string {
  return errorText(e.code === "voice_limit" ? e.reason : e.code, "Live couldn't start.");
}

function PersonTile({ participant }: { participant: Participant }) {
  const speaking = useIsSpeaking(participant);
  const name = participant.name || participant.identity;
  return (
    <span className="huddle-tile" data-speaking={speaking ? "true" : undefined} title={name}>
      <Identity name={name} avatarOnly size="default" />
    </span>
  );
}

function CeoTile({ participant, speaking }: { participant: Participant; speaking: boolean }) {
  const mics = useTracks([Track.Source.Microphone]);
  const ref = mics.find((t) => t.participant.identity === CEO_IDENTITY);
  const vol = useMultibandTrackVolume(ref, { bands: BARS });
  const latest = useRef(vol);
  useEffect(() => { latest.current = vol; }, [vol]);
  const bands = useCallback(() => latest.current, []);
  const name = LIGHTBULB;
  const reduced = useSyncExternalStore(subscribeReduced, reducedSnapshot, () => false);
  return (
    <span className="huddle-tile huddle-ceo" data-speaking={speaking ? "true" : undefined} data-motion={reduced ? "reduced" : "full"} title={name}>
      {!reduced && <AudioVisualizerRadial state={speaking ? "speaking" : "listening"} bands={bands} barCount={BARS} radius={RADIUS} />}
      <Identity name={name} avatarOnly size="default" tint={agentTint(participant.identity)} />
    </span>
  );
}

function Bar() {
  const { roomName, muted, setMuted, talking, talkStart, talkEnd, leave, error, dismissError } = useHuddle();
  const participants = useParticipants();
  const ceo = participants.find((p) => p.identity === CEO_IDENTITY);
  const people = participants.filter((p) => p.identity !== CEO_IDENTITY);
  const ceoSpeaking = useIsSpeaking(ceo);

  // A press under TAP_MS toggles talking (click mode); longer is hold-to-talk and ends on release.
  const downAt = useRef(0);
  const toggled = useRef(false);
  const onDown = (e: PointerEvent<HTMLButtonElement>) => {
    if (e.button !== 0) return;
    downAt.current = Date.now();
    if (talking) { toggled.current = true; talkEnd(); } else { toggled.current = false; talkStart(); }
  };
  const onUp = () => {
    if (!downAt.current) return;
    const tap = Date.now() - downAt.current < TAP_MS;
    downAt.current = 0;
    if (toggled.current) return; // the press that stopped a toggled talk
    if (!tap) talkEnd();
  };
  const onCancel = () => { downAt.current = 0; talkEnd(); };

  const onKey = (down: boolean) => (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key !== " ") return;
    e.preventDefault();
    if (down) { if (!e.repeat) talkStart(); } else talkEnd();
  };

  return (
    <div className="huddle-bar" role="region" aria-label={`Live in ${roomName ?? "this channel"}`}>
      <RoomAudioRenderer />
      <span className="huddle-live">🔴 Live</span>
      <div className="huddle-tiles">
        {ceo && <CeoTile participant={ceo} speaking={ceoSpeaking} />}
        {people.map((p) => <PersonTile key={p.identity} participant={p} />)}
      </div>
      {ceo ? <Status participant={ceo} speaking={ceoSpeaking} />
        : <span className="huddle-status" aria-live="polite">Lightbulb is joining…</span>}
      {error && (
        <span className="huddle-error" role="alert">
          {huddleErrorText(error)}
          <button type="button" className="huddle-button" onClick={dismissError}>Dismiss</button>
        </span>
      )}
      <button type="button" className="huddle-talk" aria-pressed={talking}
        onPointerDown={onDown} onPointerUp={onUp} onPointerLeave={onUp} onPointerCancel={onCancel} onBlur={onCancel}
        onKeyDown={onKey(true)} onKeyUp={onKey(false)}>
        Hold to talk to {LIGHTBULB}
      </button>
      <button type="button" className="huddle-button" aria-label="Mute" aria-pressed={muted} onClick={() => setMuted(!muted)}>
        {muted ? <MicOff className="h-4 w-4" aria-hidden="true" /> : <Mic className="h-4 w-4" aria-hidden="true" />}
        <span className="huddle-button-label">{muted ? "Muted" : "Mute"}</span>
      </button>
      <button type="button" className="huddle-button" data-tone="alert" aria-label="Leave Live" onClick={leave}>
        <PhoneOff className="h-4 w-4" />
      </button>
    </div>
  );
}

/** Above the composer while Live. Renders a plain error bar when the join failed before a room existed. */
export function HuddleBar() {
  const { room, error, dismissError } = useHuddle();
  if (!room) {
    return error ? (
      <div className="huddle-bar" role="alert">
        <span className="huddle-error">{huddleErrorText(error)}</span>
        <button type="button" className="huddle-button" onClick={dismissError}>Close</button>
      </div>
    ) : null;
  }
  return <RoomContext.Provider value={room}><Bar /></RoomContext.Provider>;
}
