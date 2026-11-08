"use client";

// Talk to the assistant: the one voice control, on voice Home (full) and on the Workspace rail (compact).
// One control, two gestures (founder, 8 Nov 2026; research: super-agent-design/evidence/voice/voice-input-ux-research.md):
// a tap starts a hands-free talk (mic open, OpenAI's turn detection, talk over it to interrupt); a press held past
// HOLD_MS is push-to-talk and its release ends the talk. A tap while the voice speaks stops the reply; another tap
// ends the talk. Mute and End are their own buttons; Esc ends. The Mac app's Fn / ⌃⌥Space arrives as TALK_KEY.
import "./voice.css";
import { useCallback, useEffect, useId, useRef, useState, type RefObject } from "react";
import { Mic, MicOff, Square } from "lucide-react";
import { RoomContext, useLocalParticipant, useParticipantAttributes, useRemoteParticipant, useTrackVolume, useTranscriptions } from "@livekit/components-react";
import { Track, type Participant } from "livekit-client";
import { agentLiveStatus, statusKey } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { CEO_IDENTITY, useHuddle } from "@/components/huddle/HuddleProvider";
import { useAssistantDm } from "@/components/huddle/useAssistantDm";
import { isTauri } from "@/lib/externalAuth";
import { useQuestions } from "./data";

export const HOLD_MS = 350; // Raycast's tap/hold rule; Wispr uses 500
/** The Mac app's talk key (Fn, or ⌃⌥Space until Input Monitoring is granted): detail "down" | "up". */
export const TALK_KEY = "lightbulb:talk-key";

type Orb = "idle" | "connecting" | "listening" | "thinking" | "working" | "speaking" | "muted" | "needs";
type Heard = { status: string; task: string; caption: string } | null;

/** While Live: what the Router is doing, from the worker's `lightbulb.agent.<tile>.status` and `.task` attributes
 *  (owner.py), and its last spoken line from LiveKit's transcription stream; `null` until the worker has joined and
 *  set its status. Renders nothing: it sits beside the orb, never around it, so the held button never remounts. */
function AgentHeard({ tile, onHeard }: { tile: string; onHeard: (h: Heard) => void }) {
  const agent = useRemoteParticipant(CEO_IDENTITY);
  useEffect(() => { if (!agent) onHeard(null); }, [agent, onHeard]);
  return agent ? <AgentLines agent={agent} tile={tile} onHeard={onHeard} /> : null;
}
function AgentLines({ agent, tile, onHeard }: { agent: Participant; tile: string; onHeard: (h: Heard) => void }) {
  const { attributes } = useParticipantAttributes({ participant: agent });
  const lines = useTranscriptions({ participantIdentities: [CEO_IDENTITY] });
  const status = attributes?.[statusKey(tile)] ? agentLiveStatus(attributes, tile) : null, caption = lines.at(-1)?.text ?? "";
  const task = attributes?.[`lightbulb.agent.${tile}.task`] ?? "";
  useEffect(() => { onHeard(status ? { status, task, caption } : null); }, [status, task, caption, onHeard]);
  useEffect(() => () => onHeard(null), [onHeard]);
  return null;
}

/** The live input level on the orb (`--lvl`, 0..1), written straight to the element: no re-render per frame. */
function MicLevel({ orb }: { orb: RefObject<HTMLButtonElement | null> }) {
  const { localParticipant, microphoneTrack } = useLocalParticipant();
  const volume = useTrackVolume(microphoneTrack ? { participant: localParticipant, publication: microphoneTrack, source: Track.Source.Microphone } : undefined);
  useEffect(() => { orb.current?.style.setProperty("--lvl", Math.min(1, volume * 3).toFixed(2)); }, [orb, volume]);
  return null;
}

/** A short rising two-note chime when the mic goes live (Wispr does this). No Web Audio: silent. */
function chime() {
  try {
    const ctx = new AudioContext(), t = ctx.currentTime, gain = ctx.createGain();
    gain.connect(ctx.destination);
    gain.gain.setValueAtTime(0.0001, t); gain.gain.exponentialRampToValueAtTime(0.08, t + 0.02); gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
    [660, 990].forEach((hz, i) => { const o = ctx.createOscillator(); o.type = "sine"; o.frequency.value = hz; o.connect(gain); o.start(t + i * 0.07); o.stop(t + 0.24); });
    setTimeout(() => void ctx.close(), 400);
  } catch { /* no audio output: the label says Listening */ }
}

/** The Mac app shows a floating "Mic on" pill while its window is behind (apps/desktop voice_key.rs). Older builds
 *  lack the command: ignored. */
function tellDesktop(live: boolean) {
  const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: (c: string, a: object) => Promise<unknown> } } }).__TAURI__?.core?.invoke;
  void invoke?.("voice_mic_live", { live })?.catch(() => {});
}

/** The browser already lets this site use the mic. Pre-connecting with anything less would prompt (or fail with
 *  mic_denied) on page load; the first press stays the one that asks. No Permissions API: treated as not yet.
 *  The desktop app's webview (wry/WebKit) has no Permissions API for the mic and grants getUserMedia without a
 *  prompt (#5040), so there the answer is yes. */
async function micGranted(): Promise<boolean> {
  if (isTauri()) return true;
  try { return (await navigator.permissions.query({ name: "microphone" as PermissionName })).state === "granted"; }
  catch { return false; }
}

/** Live in the owner's own Lightbulb DM, which is owner mode (the Router). One Live at a time (HuddleProvider): while
 *  you are in another room's Live the orb is off and says where you are. `compact`: a rail item whose status and
 *  caption open beside the rail while a talk is on.
 *  Pre-connect (8 Nov, prod: the agent took 21 s to join on the first press): once the DM is known the orb joins it
 *  with the mic never opened, so no audio is published until a press; the first press only opens the mic. After a
 *  talk ends the room stays warm with the mic muted (its capture stopped), so the next tap is instant. */
export function TalkOrb({ name, compact = false }: { name: string; compact?: boolean }) {
  const huddle = useHuddle();
  const room = useAssistantDm();
  const orbRef = useRef<HTMLButtonElement>(null);
  const started = useRef(false); // this orb asked for the join, so the mic on connect is ours to set
  const live = !!room && huddle.roomId === room.roomId;
  // The talk is ours to show (End, the bubble, errors): this orb was pressed since this Live began, or mounted into
  // it. A pre-connect alone is not: nothing was asked, so nothing is on, and its errors wait for the first press.
  const [mine, setMine] = useState(live);
  const busy = huddle.roomId ?? huddle.joining;
  const elsewhere = !!room && !!busy && busy !== room.roomId;
  if (elsewhere && mine) setMine(false); // another room's join is the latest: its errors are its own
  // A talk is on: the mic is open, or muted inside the talk. Mounted into an open mic (moving between places), it is.
  const [talk, setTalk] = useState(() => live && !!huddle.room?.localParticipant.isMicrophoneEnabled);
  const [muted, setMuted] = useState(false);
  const want = useRef(talk); // the mic should be open: read when a join lands
  const down = useRef<{ at: number; was: boolean } | null>(null);
  const connected = huddle.room;
  const [heard, setHeard] = useState<Heard>(null);
  const agent = live && connected ? heard : null;

  const setMic = useCallback(async (on: boolean) => {
    want.current = on;
    if (!live || !connected) return; // the join in flight reads `want` when it lands
    await connected.localParticipant.setMicrophoneEnabled(on);
    if (on) chime();
  }, [live, connected]);
  const open = () => {
    if (!room) return;
    huddle.dismissError();
    setTalk(true); setMuted(false); setMine(true); want.current = true;
    if (live) void setMic(true);
    else if (!started.current) { started.current = true; void huddle.join(room.roomId, name); }
    // else the pre-connect is in flight: it lands with `want` set and opens the mic
  };
  const end = useCallback(() => { setTalk(false); setMuted(false); void setMic(false); }, [setMic]);
  const toggleMute = () => { const m = !muted; setMuted(m); void setMic(!m); };
  const stopReply = () => void connected?.localParticipant.performRpc({ destinationIdentity: CEO_IDENTITY, method: "interrupt", payload: "" }).catch(() => {});

  const press = () => {
    if (!room || elsewhere || down.current) return;
    down.current = { at: Date.now(), was: talk };
    if (!talk) open(); // the mic opens on the press, so a push-to-talk keeps its first word
    else if (muted) void setMic(true); // held while muted: talk through the mute
  };
  const release = () => {
    const d = down.current;
    if (!d) return;
    down.current = null;
    const held = Date.now() - d.at >= HOLD_MS;
    if (!d.was) { if (held) end(); return; } // tap: the talk stays on, hands-free; hold: push-to-talk is over
    if (held) { if (muted) void setMic(false); return; }
    if (agent?.status === "speaking") stopReply(); else end();
  };
  // The Mac app's key, to the first orb on the page only (two mounted orbs would both start).
  const keyRef = useRef({ press, release });
  useEffect(() => { keyRef.current = { press, release }; });
  useEffect(() => {
    const onKey = (e: Event) => {
      if (document.querySelector(".vx-orb") !== orbRef.current) return;
      if ((e as CustomEvent).detail === "down") keyRef.current.press(); else keyRef.current.release();
    };
    window.addEventListener(TALK_KEY, onKey);
    return () => window.removeEventListener(TALK_KEY, onKey);
  }, []);
  useEffect(() => {
    if (!talk) return;
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") end(); };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [talk, end]);

  // Once per mount, once the DM is known, and only if nothing is Live: never on a later change (after idle_ended
  // the provider leaves; rejoining then would loop until the quiet limit forever). The mic stays closed.
  const preconnected = useRef(false);
  const mounted = useRef(false); // the permission check answers after this orb may be gone
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const dmId = room?.roomId, { roomId: liveId, joining, join } = huddle;
  useEffect(() => {
    if (!dmId || preconnected.current) return;
    preconnected.current = true;
    if (liveId || joining) return;
    void micGranted().then((ok) => {
      if (!ok || !mounted.current || started.current) return; // unmounted meanwhile, or a press got there first
      started.current = true; void join(dmId, name, false);
    });
  }, [dmId, liveId, joining, join, name]);
  // A join of ours landed: open the mic if a press wants it (a pre-connect joined without it), or close the one a
  // pressed join opened if the press was released meanwhile. A join of ours that another room's join superseded
  // never touches that room.
  useEffect(() => {
    if (!connected || !started.current) return;
    started.current = false;
    if (!live) return;
    if (want.current !== connected.localParticipant.isMicrophoneEnabled) void connected.localParticipant.setMicrophoneEnabled(want.current);
    if (want.current) chime();
  }, [connected, live]);
  useEffect(() => { if (huddle.error) started.current = false; }, [huddle.error]); // a failed join never connects
  // The worker ended the talk (quiet_ended: mic closed, room warm), or left (idle_ended), or the join failed.
  const code = huddle.error?.code;
  const [seenCode, setSeenCode] = useState(code);
  if (code !== seenCode) { setSeenCode(code); if (code) { setTalk(false); setMuted(false); } }
  useEffect(() => { if (code) want.current = false; }, [code]);
  const micLive = live && talk && !muted;
  useEffect(() => { if (isTauri()) tellDesktop(micLive); }, [micLive]);

  const needs = useQuestions().data?.length ?? 0;
  const tile = room?.members?.find((m) => m.kind === "agent")?.id ?? "assistant"; // the tile owner.py speaks as
  const error = mine ? huddle.error : null;
  const ended = error?.code === "quiet_ended" || error?.code === "idle_ended"; // not failures: a polite line
  const err = error && ({
    mic_denied: "Allow the microphone in your browser to talk.", no_mic: "No microphone found.",
    voice_unavailable: "Talking isn't switched on for this workspace yet.", workspace_not_ready: "Your workspace is still starting. Try again in a minute.",
    quiet_ended: "Ended after a quiet moment. Tasks keep running. Tap to talk again.",
    idle_ended: `${name} left after a few minutes of quiet. Tap to talk again.`, connection_lost: "The connection dropped. Tap to talk again.",
  } as Record<string, string>)[error.code];
  const on = live && mine && talk;
  const state: Orb = mine && talk && ((room && huddle.joining === room.roomId) || (live && !agent)) ? "connecting"
    : !on ? "idle"
    : agent?.status === "speaking" ? "speaking"
    : agent?.status === "working" ? (agent.task ? "working" : "thinking")
    : muted ? "muted" : needs > 0 ? "needs" : "listening";
  const caption = agent?.caption;
  const alert = ended ? "" : err || (error ? `Couldn't reach ${name}. Try again.` : "");
  const away = elsewhere ? `You're in a Live in ${huddle.roomName ?? "another room"}. End it to talk to ${name}.` : "";
  const label = {
    idle: `Tap to talk to ${name}`, connecting: `Connecting to ${name}…`, listening: "Listening",
    thinking: "Thinking…", working: agent?.task || "Working…", speaking: `${name} is speaking · tap to stop`,
    muted: "Muted", needs: `Listening · ${needs} need${needs === 1 ? "s" : ""} you on screen`,
  }[state];
  const lineId = useId();
  const controls = on && <div className="vx-controls">
    <Button type="button" size="sm" variant="outline" aria-pressed={muted} onClick={toggleMute}>{muted ? "Unmute" : "Mute"}</Button>
    <Button type="button" size="sm" variant="outline" onClick={end} aria-keyshortcuts="Escape">End</Button>
  </div>;
  const Icon = state === "speaking" ? Square : state === "muted" ? MicOff : Mic;
  return <div className="vx-talk" data-compact={compact || undefined}>
    <button ref={orbRef} type="button" className="vx-orb" data-state={state} disabled={room === null || elsewhere}
      aria-label={`Talk to ${name}`} aria-pressed={on} aria-describedby={away ? lineId : undefined}
      title={away || "Tap to talk · hold to push-to-talk"}
      onPointerDown={(e) => { if (e.button !== 0) return; e.currentTarget.setPointerCapture(e.pointerId); press(); }}
      onPointerUp={release} onPointerCancel={release} onContextMenu={(e) => e.preventDefault()}
      onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); press(); } }}
      onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") release(); }}>
      <Icon size={compact ? 20 : state === "speaking" ? 18 : 22} aria-hidden="true" fill={state === "speaking" ? "currentColor" : "none"} />
      {compact && <span aria-hidden="true">Talk</span>}
    </button>
    {/* compact: a bubble beside the rail, open while a talk is on; closed it stays in the accessibility tree */}
    <div className="vx-talk-text" data-open={compact ? on || state !== "idle" || !!alert || ended : undefined}>
      <b role="status" aria-live="polite" data-state={state}>{label}</b>
      {/* one persistent line: an alert, else a polite region with the caption, the ending, or the suggestions */}
      <p id={lineId} role={alert ? "alert" : undefined} aria-live={alert ? undefined : "polite"} className={!alert && on && caption ? "vx-caption" : undefined}>
        {alert || (on && caption) || (ended && err) || away || (room === null ? `${name} isn't set up in this workspace yet.`
          : <>“What happened today?”<span className="vx-more"> · “Move Priya’s 11 to 11:30 and text her” · “Draft the holiday-hours post”</span></>)}</p>
      {compact && controls}
    </div>
    {!compact && controls}
    {live && connected && <RoomContext.Provider value={connected}>
      <AgentHeard tile={tile} onHeard={setHeard} />
      {micLive && <MicLevel orb={orbRef} />}
    </RoomContext.Provider>}
  </div>;
}
