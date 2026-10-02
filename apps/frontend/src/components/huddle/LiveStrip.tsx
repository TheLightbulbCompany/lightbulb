"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { PhoneOff, X } from "lucide-react";
import { Track, type Participant } from "livekit-client";
import {
  BarVisualizer, RoomContext, StartAudio, TrackToggle, useIsSpeaking, useParticipantAttributes, useParticipants, useTracks,
  type AgentState,
} from "@livekit/components-react";
import { Identity } from "@/components/teams/shared/components/Identity";
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { agentTint } from "@/lib/agentTint";
import { liveErrorText, liveStatus } from "@lightbulb/shared";
import { CEO_IDENTITY, useHuddle } from "./HuddleProvider";

const LIGHTBULB = "Lightbulb";
const TALK = `Talk to ${LIGHTBULB}`;
const HAND = `${LIGHTBULB} has something; click to hear it`;
const FACES = 3;
const BARS = 5;
const JOIN_TIMEOUT_MS = 45_000; // the worker joins in ~20 s; past this it isn't coming
const REDUCED = "(prefers-reduced-motion: reduce)";
const subscribeReduced = (onChange: () => void) => {
  const mql = window.matchMedia(REDUCED);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
};
const useReducedMotion = () => useSyncExternalStore(subscribeReduced, () => window.matchMedia(REDUCED).matches, () => false);

/** The BarVisualizer's state for the worker's status. Reduced motion: no stepping sequence, all bars lit while speaking. */
function vizState(status: string | undefined, speaking: boolean, reduced: boolean): AgentState | undefined {
  if (reduced) return speaking ? undefined : "idle";
  if (speaking || status === "speaking") return "speaking";
  if (status === "thinking" || status === "working") return "thinking";
  return status === "listening" ? "listening" : "idle";
}

/**
 * Lightbulb's audio. A click asks Lightbulb in (one talk_start, no hold); saying "Lightbulb, …" does the same.
 * A raised hand (`lightbulb.hand`, the worker's 15 s offer) means it has news or an answer; the same click hears it.
 */
function LightbulbBars({ participant, onTalk }: { participant: Participant; onTalk: () => void }) {
  const speaking = useIsSpeaking(participant);
  const { attributes } = useParticipantAttributes({ participant });
  const track = useTracks([Track.Source.Microphone]).find((t) => t.participant.identity === CEO_IDENTITY);
  const reduced = useReducedMotion();
  const hand = attributes?.["lightbulb.hand"] === "1";
  const label = hand ? HAND : TALK;
  return (
    <button type="button" className="live-viz" aria-label={label} title={label} onClick={onTalk}
      data-speaking={speaking ? "true" : undefined} data-hand={hand ? "true" : undefined}>
      <BarVisualizer barCount={BARS} state={vizState(attributes?.["lightbulb.status"], speaking, reduced)}
        track={reduced ? undefined : track} options={{ minHeight: 20, maxHeight: 100 }} />
      {hand && <span className="live-hand" aria-hidden="true">✋</span>}
    </button>
  );
}

function JoiningBars() {
  const reduced = useReducedMotion();
  return (
    <span className="live-viz" data-joining="true" title={`${LIGHTBULB} is joining…`}>
      <span className="sr-only">{LIGHTBULB} is joining</span>
      <BarVisualizer barCount={BARS} state={reduced ? "idle" : "connecting"} options={{ minHeight: 20, maxHeight: 100 }} />
    </span>
  );
}

/** useIsSpeaking throws without a participant (it crashed prod once), so this only mounts once Lightbulb has joined. */
function LightbulbStatus({ participant }: { participant: Participant }) {
  const speaking = useIsSpeaking(participant);
  const { attributes } = useParticipantAttributes({ participant });
  return <>{speaking ? "Speaking" : liveStatus(attributes)}</>;
}

function Face({ participant, stack }: { participant: Participant; stack: number }) {
  const speaking = useIsSpeaking(participant);
  const name = participant.name || participant.identity;
  return (
    <span className="live-face" data-speaking={speaking ? "true" : undefined} title={name} style={{ zIndex: stack }}>
      <Identity name={name} avatarOnly size="sm" />
    </span>
  );
}

function PersonRow({ participant }: { participant: Participant }) {
  const speaking = useIsSpeaking(participant);
  const name = participant.name || participant.identity;
  return (
    <li className="live-row">
      <span className="live-face" data-speaking={speaking ? "true" : undefined}><Identity name={name} avatarOnly size="sm" /></span>
      <span className="live-row-name">{name}{participant.isLocal ? " (you)" : ""}</span>
      {speaking && <span className="live-row-meta">Speaking</span>}
    </li>
  );
}

function Mic() {
  // LiveKit's own toggle: aria-pressed is "mic on"; muted means the room can't hear you.
  return <TrackToggle source={Track.Source.Microphone} className="live-icon" aria-label="Microphone" title="Microphone" />;
}

function Leave({ onLeave }: { onLeave: () => void }) {
  // Not DisconnectButton: its disconnect fires RoomEvent.Disconnected, which the provider reads as a dropped call.
  return (
    <button type="button" className="live-icon" data-tone="alert" aria-label="Leave Live" title="Leave Live" onClick={onLeave}>
      <PhoneOff className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

function Strip({ here }: { here: boolean }) {
  const { roomId, roomName, lightbulb, join, leave, talkStart, error, dismissError } = useHuddle();
  const participants = useParticipants();
  const ceo = participants.find((p) => p.identity === CEO_IDENTITY);
  const people = participants.filter((p) => p.identity !== CEO_IDENTITY);
  const [open, setOpen] = useState(false);
  const [gaveUp, setGaveUp] = useState(false);
  useEffect(() => {
    if (ceo || !lightbulb) return; // a people-only room: Lightbulb isn't coming, so nothing to give up on
    const t = setTimeout(() => { setGaveUp(true); setOpen(true); }, JOIN_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [ceo, lightbulb]);
  // A new error opens the details so it is seen (adjusting state during render, not in an effect).
  const [shownError, setShownError] = useState(error);
  if (shownError !== error) { setShownError(error); if (error) setOpen(true); }
  const channel = roomName || "this room";
  const startAudio = <StartAudio label="Turn on sound" className="live-pill" />;

  if (!here) {
    return (
      <div className="live-strip" role="group" aria-label={`Live in ${channel}`}>
        {startAudio}
        <button type="button" className="live-summary" title={`Return to ${channel}`}
          onClick={() => window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId, threadId: null } }))}>
          <span className="live-dot" aria-hidden="true" />
          <span className="live-label">Live in {channel}</span>
          <span className="live-return">Return</span>
        </button>
        <Mic />
        <Leave onLeave={leave} />
      </div>
    );
  }

  const shown = people.slice(0, FACES);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <div className="live-strip" role="group" aria-label={`Live in ${channel}`}>
          {startAudio}
          <PopoverTrigger asChild>
            <button type="button" className="live-summary" aria-label={`Live in ${channel}, ${participants.length} here. Details`}>
              <span className="live-dot" aria-hidden="true" />
              <span className="live-label">Live</span>
              <span className="live-faces" aria-hidden="true">
                {shown.map((p, i) => <Face key={p.identity} participant={p} stack={FACES - i} />)}
                {people.length > shown.length && <span className="live-more">+{people.length - shown.length}</span>}
              </span>
              {error && <span className="live-alert-dot" aria-hidden="true" />}
            </button>
          </PopoverTrigger>
          {ceo ? <LightbulbBars participant={ceo} onTalk={talkStart} /> : lightbulb && <JoiningBars />}
          <Mic />
          <Leave onLeave={leave} />
          {ceo && <span className="sr-only" aria-live="polite"><LightbulbStatus participant={ceo} /></span>}
        </div>
      </PopoverAnchor>
      <PopoverContent align="end" className="live-popover">
        <p className="live-popover-title">Live in {channel}</p>
        {(ceo || lightbulb) && <div className="live-row">
          <span className="live-face"><Identity name={LIGHTBULB} avatarOnly size="sm" tint={agentTint(CEO_IDENTITY)} /></span>
          <span className="live-row-name">{LIGHTBULB}</span>
          <span className="live-row-meta">
            {ceo ? <LightbulbStatus participant={ceo} /> : gaveUp ? "Didn't join" : "Joining…"}
          </span>
        </div>}
        {!ceo && lightbulb && gaveUp && (
          <p className="live-note" role="alert">
            {LIGHTBULB} didn&apos;t join.
            <button type="button" className="live-pill" onClick={() => { if (roomId) void join(roomId, roomName ?? ""); }}>Retry</button>
          </p>
        )}
        {error && (
          <p className="live-note" role="alert">
            {liveErrorText(error)}
            <button type="button" className="live-pill" onClick={dismissError}>Dismiss</button>
          </p>
        )}
        <ul className="live-people" aria-label="People in Live">
          {people.map((p) => <PersonRow key={p.identity} participant={p} />)}
        </ul>
        {(ceo || lightbulb) && <p className="live-hint">Say &ldquo;{LIGHTBULB}, …&rdquo; or click the bars to talk to {LIGHTBULB}.</p>}
      </PopoverContent>
    </Popover>
  );
}

/**
 * Live in the room header: the full strip in the Live room's own header, a compact "Live in #x — Return" in any
 * other, so it can always be found and left. A join that failed before a room existed shows its error instead.
 */
export function LiveStrip({ roomId }: { roomId: string | undefined }) {
  const { room, roomId: liveRoomId, error, dismissError } = useHuddle();
  if (!room) {
    return error ? (
      <span className="live-note" role="alert">
        {liveErrorText(error)}
        <button type="button" className="live-icon" aria-label="Dismiss" onClick={dismissError}><X className="h-4 w-4" aria-hidden="true" /></button>
      </span>
    ) : null;
  }
  return <RoomContext.Provider value={room}><Strip here={liveRoomId === roomId} /></RoomContext.Provider>;
}
