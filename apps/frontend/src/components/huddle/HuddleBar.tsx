"use client";

import { useCallback, useEffect, useRef, type KeyboardEvent } from "react";
import { Mic, MicOff, PhoneOff } from "lucide-react";
import { Track, type Participant } from "livekit-client";
import { RoomAudioRenderer, RoomContext, useIsSpeaking, useMultibandTrackVolume, useParticipants, useTracks } from "@livekit/components-react";
import { Identity } from "@/components/teams/shared/components/Identity";
import { agentTint } from "@/lib/agentTint";
import { AudioVisualizerRadial } from "@/components/voice/AudioVisualizerRadial";
import { ERRORS } from "@/components/voice/VoiceOrb";
import { CEO_IDENTITY, useHuddle, type HuddleError } from "./HuddleProvider";

const BARS = 24;

export function huddleErrorText(e: HuddleError): string {
  return ERRORS[e.code === "voice_limit" ? (e.reason ?? "") : e.code] ?? "The huddle couldn't start.";
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
  const name = participant.name || "CEO";
  return (
    <span className="huddle-tile huddle-ceo" data-speaking={speaking ? "true" : undefined} title={name}>
      <AudioVisualizerRadial state={speaking ? "speaking" : "listening"} bands={bands} barCount={BARS} />
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
  const talker = people.find((p) => p.isSpeaking);
  const ceoName = ceo?.name || "the CEO";
  const status = ceoSpeaking ? "Speaking" : talker ? `Listening to ${talker.name || talker.identity}` : "Following along";

  const onKey = (down: boolean) => (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key !== " ") return;
    e.preventDefault();
    if (down) { if (!e.repeat) talkStart(); } else talkEnd();
  };

  return (
    <div className="huddle-bar" role="region" aria-label={`Huddle in ${roomName ?? "this channel"}`}>
      <RoomAudioRenderer />
      <div className="huddle-tiles">
        {ceo && <CeoTile participant={ceo} speaking={ceoSpeaking} />}
        {people.map((p) => <PersonTile key={p.identity} participant={p} />)}
      </div>
      <span className="huddle-status" aria-live="polite">{status}</span>
      {error && (
        <span className="huddle-error" role="alert">
          {huddleErrorText(error)}
          <button type="button" className="huddle-button" onClick={dismissError}>Dismiss</button>
        </span>
      )}
      <button type="button" className="huddle-talk" aria-pressed={talking}
        onPointerDown={talkStart} onPointerUp={talkEnd} onPointerLeave={talkEnd} onPointerCancel={talkEnd} onBlur={talkEnd}
        onKeyDown={onKey(true)} onKeyUp={onKey(false)}>
        Hold to talk to {ceoName}
      </button>
      <button type="button" className="huddle-button" aria-label="Mute" aria-pressed={muted} onClick={() => setMuted(!muted)}>
        {muted ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
      </button>
      <button type="button" className="huddle-button" data-tone="alert" aria-label="Leave huddle" onClick={leave}>
        <PhoneOff className="h-4 w-4" />
      </button>
    </div>
  );
}

/** Above the composer while in a huddle. Renders a plain error bar when the join failed before a room existed. */
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
