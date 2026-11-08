"use client";

// Hold to talk to the assistant: the one voice control, on voice Home (full) and on the Workspace rail (compact).
import "./voice.css";
import { useEffect, useId, useRef, useState } from "react";
import { Mic } from "lucide-react";
import { RoomContext, useParticipantAttributes, useRemoteParticipant, useTranscriptions } from "@livekit/components-react";
import type { Participant } from "livekit-client";
import { agentLiveStatus, statusKey } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { CEO_IDENTITY, useHuddle } from "@/components/huddle/HuddleProvider";
import { useAssistantDm } from "@/components/huddle/useAssistantDm";

type Orb = "idle" | "connecting" | "listening" | "thinking" | "speaking";
type Heard = { status: string; caption: string } | null;

/** While Live: what the Router is doing, from the worker's `lightbulb.agent.<tile>.status` attribute (owner.py),
 *  and its last spoken line from LiveKit's transcription stream; `null` until the worker has joined and set that
 *  attribute (it is in the room seconds before it listens). Renders nothing: it sits beside the orb, never around
 *  it, so the held button never remounts. */
function AgentHeard({ tile, onHeard }: { tile: string; onHeard: (h: Heard) => void }) {
  const agent = useRemoteParticipant(CEO_IDENTITY);
  useEffect(() => { if (!agent) onHeard(null); }, [agent, onHeard]);
  return agent ? <AgentLines agent={agent} tile={tile} onHeard={onHeard} /> : null;
}
function AgentLines({ agent, tile, onHeard }: { agent: Participant; tile: string; onHeard: (h: Heard) => void }) {
  const { attributes } = useParticipantAttributes({ participant: agent });
  const lines = useTranscriptions({ participantIdentities: [CEO_IDENTITY] });
  const status = attributes?.[statusKey(tile)] ? agentLiveStatus(attributes, tile) : null, caption = lines.at(-1)?.text ?? "";
  useEffect(() => { onHeard(status ? { status, caption } : null); }, [status, caption, onHeard]);
  useEffect(() => () => onHeard(null), [onHeard]);
  return null;
}

/** The browser already lets this site use the mic. Pre-connecting with anything less would prompt (or fail with
 *  mic_denied) on page load; the first press stays the one that asks. No Permissions API: treated as not yet. */
async function micGranted(): Promise<boolean> {
  try { return (await navigator.permissions.query({ name: "microphone" as PermissionName })).state === "granted"; }
  catch { return false; }
}

/** Hold to talk: Live in the owner's own Lightbulb DM, which is owner mode (the Router listens to the mic and
 *  takes the turn when it goes quiet); release mutes the mic. One Live at a time (HuddleProvider): while you are
 *  in another room's Live the orb is off and says where you are, so it never takes that Live over. `compact`: a
 *  rail item whose status and caption open beside the rail while a talk is on.
 *  Pre-connect (8 Nov, prod: the agent took 21 s to join on the first press, the hold was long over): once the DM is
 *  known the orb joins it with the mic off, so the first press only unmutes. Until that press the orb looks idle. */
export function TalkOrb({ name, compact = false }: { name: string; compact?: boolean }) {
  const huddle = useHuddle();
  const held = useRef(false);
  const started = useRef(false); // this orb asked for the join, so the mic on connect is ours to set
  const room = useAssistantDm();
  const live = !!room && huddle.roomId === room.roomId;
  // The talk is ours to show (End, the bubble, errors): the member held since this Live began, or mounted into it.
  // A pre-connect alone is not: nothing was asked, so nothing is on, and its errors wait for the first press.
  const [mine, setMine] = useState(live);
  const busy = huddle.roomId ?? huddle.joining;
  const elsewhere = !!room && !!busy && busy !== room.roomId;
  if (elsewhere && mine) setMine(false); // another room's join is the latest: its errors are its own
  const [talking, setTalking] = useState(false);
  const press = async () => {
    if (!room || elsewhere || held.current) return;
    held.current = true; setTalking(true); setMine(true);
    if (live) await huddle.room?.localParticipant.setMicrophoneEnabled(true);
    else if (!started.current) { started.current = true; await huddle.join(room.roomId, name); }
    // else the pre-connect is in flight: its join turns the mic on, and `held` keeps it on
  };
  const release = () => {
    if (!held.current) return;
    held.current = false; setTalking(false);
    void huddle.room?.localParticipant.setMicrophoneEnabled(false);
  };
  // Once per mount, once the DM is known, and only if nothing is Live: never on a later change (after idle_ended
  // the provider leaves; rejoining then would loop until the quiet limit forever).
  const preconnected = useRef(false);
  const dmId = room?.roomId, { roomId: liveId, joining, join } = huddle;
  useEffect(() => {
    if (!dmId || preconnected.current) return;
    preconnected.current = true;
    if (liveId || joining) return;
    void micGranted().then((ok) => {
      if (!ok || started.current) return; // a press got there first
      started.current = true; void join(dmId, name);
    });
  }, [dmId, liveId, joining, join, name]);
  // Join turns the mic on; not held when it connects (released while connecting, or a pre-connect), it goes straight
  // back off. A join of ours that another room's join superseded never mutes that room.
  const connected = huddle.room;
  useEffect(() => {
    if (!connected || !started.current) return;
    started.current = false;
    if (live && !held.current) void connected.localParticipant.setMicrophoneEnabled(false);
  }, [connected, live]);
  useEffect(() => { if (huddle.error) started.current = false; }, [huddle.error]); // a failed join never connects
  const tile = room?.members?.find((m) => m.kind === "agent")?.id ?? "assistant"; // the tile owner.py speaks as
  const error = mine ? huddle.error : null;
  const err = error && ({
    mic_denied: "Allow the microphone in your browser to talk.", no_mic: "No microphone found.",
    voice_unavailable: "Talking isn't switched on for this workspace yet.", workspace_not_ready: "Your workspace is still starting. Try again in a minute.",
    // owner.py ends a talk after AWAY_S of quiet and says so; that is not a failure (8 Nov: it read "Couldn't reach").
    idle_ended: `${name} left after a few minutes of quiet. Hold to talk again.`, connection_lost: "The connection dropped. Hold to talk again.",
  } as Record<string, string>)[error.code];
  const [heard, setHeard] = useState<Heard>(null);
  const agent = live && connected ? heard : null;
  const on = live && mine; // a talk is on (a bare pre-connect looks idle)
  const state: Orb = mine && ((room && huddle.joining === room.roomId) || (live && !agent)) ? "connecting"
    : agent?.status === "speaking" ? "speaking" : agent?.status === "working" ? "thinking"
    : talking && live ? "listening" : "idle";
  const caption = agent?.caption;
  const alert = err || (error ? `Couldn't reach ${name}. Try again.` : "");
  const away = elsewhere ? `You're in a Live in ${huddle.roomName ?? "another room"}. End it to talk to ${name}.` : "";
  const lineId = useId();
  const end = on && <Button type="button" size="sm" variant="outline" onClick={() => { release(); huddle.leave(); }}>End</Button>;
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
    <div className="vx-talk-text" data-open={compact ? on || state !== "idle" || !!alert : undefined}>
      <b aria-live="polite">{{ connecting: `Connecting to ${name}… the first time can take 20 s — keep holding`, listening: `${name} is listening`, thinking: `${name} is thinking…`,
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
