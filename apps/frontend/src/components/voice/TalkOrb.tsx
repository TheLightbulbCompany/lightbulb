"use client";

// Hold to talk to the assistant: the one voice control, on voice Home (full) and on the Workspace rail (compact).
import "./voice.css";
import { useEffect, useId, useRef, useState } from "react";
import { Mic } from "lucide-react";
import { RoomContext, useParticipantAttributes, useRemoteParticipant, useTranscriptions } from "@livekit/components-react";
import type { Participant } from "livekit-client";
import { agentLiveStatus } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { CEO_IDENTITY, useHuddle } from "@/components/huddle/HuddleProvider";
import { useAssistantDm } from "@/components/huddle/useAssistantDm";

type Orb = "idle" | "connecting" | "listening" | "thinking" | "speaking";
type Heard = { status: string; caption: string } | null;

/** While Live: what the Router is doing, from the worker's `lightbulb.agent.<tile>.status` attribute (owner.py),
 *  and its last spoken line from LiveKit's transcription stream; `null` until the worker has joined. Renders
 *  nothing: it sits beside the orb, never around it, so the held button never remounts. */
function AgentHeard({ tile, onHeard }: { tile: string; onHeard: (h: Heard) => void }) {
  const agent = useRemoteParticipant(CEO_IDENTITY);
  useEffect(() => { if (!agent) onHeard(null); }, [agent, onHeard]);
  return agent ? <AgentLines agent={agent} tile={tile} onHeard={onHeard} /> : null;
}
function AgentLines({ agent, tile, onHeard }: { agent: Participant; tile: string; onHeard: (h: Heard) => void }) {
  const { attributes } = useParticipantAttributes({ participant: agent });
  const lines = useTranscriptions({ participantIdentities: [CEO_IDENTITY] });
  const status = agentLiveStatus(attributes, tile), caption = lines.at(-1)?.text ?? "";
  useEffect(() => { onHeard({ status, caption }); }, [status, caption, onHeard]);
  useEffect(() => () => onHeard(null), [onHeard]);
  return null;
}

/** Hold to talk: Live in the owner's own Lightbulb DM, which is owner mode (the Router listens to the mic and
 *  takes the turn when it goes quiet); release mutes the mic. One Live at a time (HuddleProvider): while you are
 *  in another room's Live the orb is off and says where you are, so it never takes that Live over. `compact`: a
 *  rail item whose status and caption open beside the rail while a talk is on. */
export function TalkOrb({ name, compact = false }: { name: string; compact?: boolean }) {
  const huddle = useHuddle();
  const held = useRef(false);
  const started = useRef(false); // this orb asked for the join, so the mic on connect is ours to set
  const [mine, setMine] = useState(false); // the last join was ours: its errors are ours to show
  const room = useAssistantDm();
  const live = !!room && huddle.roomId === room.roomId;
  const busy = huddle.roomId ?? huddle.joining;
  const elsewhere = !!room && !!busy && busy !== room.roomId;
  if (elsewhere && mine) setMine(false); // another room's join is the latest: its errors are its own
  const [talking, setTalking] = useState(false);
  const press = async () => {
    if (!room || elsewhere || held.current) return;
    held.current = true; setTalking(true); setMine(true);
    if (!live) { started.current = true; await huddle.join(room.roomId, name); }
    else await huddle.room?.localParticipant.setMicrophoneEnabled(true);
  };
  const release = () => {
    if (!held.current) return;
    held.current = false; setTalking(false);
    void huddle.room?.localParticipant.setMicrophoneEnabled(false);
  };
  // Join turns the mic on; released while connecting, it goes straight back off.
  const connected = huddle.room;
  useEffect(() => {
    if (!connected || !started.current) return;
    started.current = false;
    if (!held.current) void connected.localParticipant.setMicrophoneEnabled(false);
  }, [connected]);
  useEffect(() => { if (huddle.error) started.current = false; }, [huddle.error]); // a failed join never connects
  const tile = room?.members?.find((m) => m.kind === "agent")?.id ?? "assistant"; // the tile owner.py speaks as
  const error = mine ? huddle.error : null;
  const err = error && ({
    mic_denied: "Allow the microphone in your browser to talk.", no_mic: "No microphone found.",
    voice_unavailable: "Talking isn't switched on for this workspace yet.", workspace_not_ready: "Your workspace is still starting. Try again in a minute.",
  } as Record<string, string>)[error.code];
  const [heard, setHeard] = useState<Heard>(null);
  const agent = live && connected ? heard : null;
  const state: Orb = (room && huddle.joining === room.roomId) || (live && !agent) ? "connecting"
    : agent?.status === "speaking" ? "speaking" : agent?.status === "working" ? "thinking"
    : talking && live ? "listening" : "idle";
  const caption = agent?.caption;
  const alert = err || (error ? `Couldn't reach ${name}. Try again.` : "");
  const away = elsewhere ? `You're in a Live in ${huddle.roomName ?? "another room"}. End it to talk to ${name}.` : "";
  const lineId = useId();
  const end = live && <Button type="button" size="sm" variant="outline" onClick={() => { release(); huddle.leave(); }}>End</Button>;
  return <div className="vx-talk" data-compact={compact || undefined}>
    <button type="button" className="vx-orb" data-state={state} disabled={room === null || elsewhere}
      aria-label={`Hold to talk to ${name}`} aria-pressed={talking} aria-describedby={away ? lineId : undefined} title={away || undefined}
      onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); void press(); }}
      onPointerUp={release} onPointerCancel={release} onContextMenu={(e) => e.preventDefault()}
      onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); void press(); } }}
      onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") release(); }}>
      <Mic size={compact ? 20 : state === "idle" ? 22 : 24} aria-hidden="true" />
      {compact && <span aria-hidden="true">Talk</span>}
    </button>
    {/* compact: a bubble beside the rail, open while a talk is on; closed it stays in the accessibility tree */}
    <div className="vx-talk-text" data-open={compact ? live || state !== "idle" || !!alert : undefined}>
      <b aria-live="polite">{{ connecting: `Connecting to ${name}…`, listening: `${name} is listening`, thinking: `${name} is thinking…`,
        speaking: `${name} is speaking`, idle: `Hold to talk to ${name}` }[state]}</b>
      {/* one persistent line: an alert, else a polite region with the caption or the suggestions */}
      <p id={lineId} role={alert ? "alert" : undefined} aria-live={alert ? undefined : "polite"} className={!alert && caption ? "vx-caption" : undefined}>
        {alert || caption || away || (room === null ? `${name} isn't set up in this workspace yet.`
          : <>“What happened today?”<span className="vx-more"> · “Move Priya’s 11 to 11:30 and text her” · “Draft the holiday-hours post”</span></>)}</p>
      {compact && end}
    </div>
    {!compact && end}
    {live && connected && <RoomContext.Provider value={connected}><AgentHeard tile={tile} onHeard={setHeard} /></RoomContext.Provider>}
  </div>;
}
