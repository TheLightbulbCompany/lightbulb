"use client";

// One visual control for the persistent talk session: tap for hands-free, hold for push-to-talk.
import "./voice.css";
import { useEffect, useId, useRef, type RefObject } from "react";
import { Mic, MicOff } from "lucide-react";
import { RoomContext, useLocalParticipant, useTrackVolume } from "@livekit/components-react";
import { Track } from "livekit-client";
import { Button } from "@/components/ui/button";
import { TalkSessionProvider, useTalkSession } from "./TalkSessionProvider";
export { HOLD_MS, TALK_KEY } from "./TalkSessionProvider";

/** The live input level on the orb (`--lvl`, 0..1), written straight to the element: no re-render per frame. */
function MicLevel({ orb }: { orb: RefObject<HTMLButtonElement | null> }) {
  const { localParticipant, microphoneTrack } = useLocalParticipant();
  const volume = useTrackVolume(microphoneTrack ? { participant: localParticipant, publication: microphoneTrack, source: Track.Source.Microphone } : undefined);
  useEffect(() => { orb.current?.style.setProperty("--lvl", Math.min(1, volume * 3).toFixed(2)); }, [orb, volume]);
  return null;
}

export function TalkOrb({ name, compact = false }: { name: string; compact?: boolean }) {
  const session = useTalkSession();
  // The legacy workspace rail is itself persistent. Outside VoiceShell it owns this same controller.
  return session ? <TalkOrbView compact={compact} />
    : <TalkSessionProvider name={name}><TalkOrbView compact={compact} /></TalkSessionProvider>;
}

function TalkOrbView({ compact }: { compact: boolean }) {
  const session = useTalkSession();
  if (!session) throw new Error("TalkOrb requires a talk session");
  const { name, room, live, connected, micLive, on, state, label, caption, alert, ended, err, away, muted,
    press, release, cancel, toggleMute, end, elsewhere } = session;
  const orbRef = useRef<HTMLButtonElement>(null);
  const lineId = useId();
  const controls = on && <div className="vx-controls">
    <Button type="button" size="sm" variant="outline" aria-pressed={muted} onClick={toggleMute}>{muted ? "Unmute" : "Mute"}</Button>
    <Button type="button" size="sm" variant="outline" onClick={end} aria-keyshortcuts="Escape">End</Button>
  </div>;
  const Icon = on && muted ? MicOff : Mic;
  return <div className="vx-talk" data-compact={compact || undefined}>
    <button ref={orbRef} type="button" className="vx-orb" data-state={state} disabled={room === null || elsewhere}
      aria-label={`Talk to ${name}`} aria-pressed={micLive} aria-describedby={away ? lineId : undefined}
      title={away || (micLive ? "Tap to mute microphone · hold to push-to-talk" : "Tap to talk · hold to push-to-talk")}
      onPointerDown={(e) => { if (e.button !== 0) return; e.currentTarget.setPointerCapture(e.pointerId); press(); }}
      onPointerUp={release} onPointerCancel={cancel} onContextMenu={(e) => e.preventDefault()}
      onClick={(e) => { if (e.detail === 0) { press(); release(); } }}
      onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); press(); } }}
      onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") release(); }}>
      <Icon size={compact ? 20 : 22} aria-hidden="true" />
      {compact && <span aria-hidden="true">Talk</span>}
    </button>
    {/* compact: a bubble beside the rail, open while a talk is on; closed it stays in the accessibility tree */}
    <div className="vx-talk-text" data-open={compact ? on || state !== "idle" || !!alert || ended : undefined}>
      <b role="status" aria-live="polite" data-state={state}>{label}</b>
      {/* one persistent line: an alert, else a polite region with the caption, the ending, or the suggestions */}
      <p id={lineId} role={alert ? "alert" : undefined} aria-live={alert ? undefined : "polite"} className={!alert && on && caption ? "vx-caption" : undefined}>
        {alert || (on && caption) || (ended && err) || away || (room === null ? `${name} isn't set up in this workspace yet.`
          : <>“What happened today?”<span className="vx-more"> · “Who called this morning?” · “Move Priya’s 11 to 11:30”</span></>)}</p>
      {compact && controls}
    </div>
    {!compact && controls}
    {live && connected && <RoomContext.Provider value={connected}>
      {micLive && <MicLevel orb={orbRef} />}
    </RoomContext.Provider>}
  </div>;
}
