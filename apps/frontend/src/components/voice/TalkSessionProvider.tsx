"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { RoomContext, useParticipantAttributes, useRemoteParticipant, useTranscriptions } from "@livekit/components-react";
import type { Participant } from "livekit-client";
import { agentLiveStatus, statusKey } from "@lightbulb/shared";
import { CEO_IDENTITY, useHuddle } from "@/components/huddle/HuddleProvider";
import { useAssistantDm } from "@/components/huddle/useAssistantDm";
import { isTauri } from "@/lib/externalAuth";
import { useQuestions } from "./data";

export const HOLD_MS = 350;
/** Fn / Ctrl+Alt+Space from the native app: down, up, or cancel for a chord. */
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

/** Asks for the mic on the tap itself, so the browser's prompt shows now and a refusal is known before any join.
 *  No getUserMedia (the desktop webview, tests): nothing to ask, the join does its own. */
const canAsk = () => !isTauri() && !!navigator.mediaDevices?.getUserMedia;
async function askMic(): Promise<"ok" | "denied" | "none"> {
  try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach((t) => t.stop()); return "ok"; }
  catch (e) { return (e as { name?: string }).name === "NotFoundError" ? "none" : "denied"; }
}
const MIC_ASK = "Allow microphone access to talk";
const MIC_BLOCKED = "Microphone is blocked. Click the lock icon in the address bar, set Microphone to Allow, then tap again.";

function useTalkController(name: string) {
  const huddle = useHuddle();
  const room = useAssistantDm();
  const mounted = useRef(false);
  const ownedRoom = useRef<ReturnType<typeof useHuddle>["room"]>(null);
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
  const [asking, setAsking] = useState(false); // the browser's mic prompt is open (or about to be)
  const [micErr, setMicErr] = useState("");
  const askId = useRef(0); // a newer tap, or a cancel, voids an older prompt's answer
  const want = useRef(talk); // the mic should be open: read when a join lands
  const down = useRef<{ at: number; was: boolean; muted: boolean } | null>(null);
  const connected = huddle.room;
  const [heard, setHeard] = useState<Heard>(null);
  const agent = live && connected ? heard : null;

  const setMic = useCallback(async (on: boolean) => {
    want.current = on;
    if (!live || !connected) return; // the join in flight reads `want` when it lands
    await connected.localParticipant.setMicrophoneEnabled(on);
    if (on && (!mounted.current || !want.current)) {
      await connected.localParticipant.setMicrophoneEnabled(false);
      return;
    }
    if (on) chime();
  }, [live, connected]);
  const open = () => {
    if (!room) return;
    huddle.dismissError(); setMicErr("");
    setTalk(true); setMuted(false); setMine(true); want.current = true;
    if (live) void setMic(true);
    else if (!started.current) {
      started.current = true;
      if (!canAsk()) { void huddle.join(room.roomId, name); return; }
      const id = ++askId.current, roomId = room.roomId;
      setAsking(true);
      void askMic().then((r) => {
        if (id !== askId.current || !mounted.current) return;
        setAsking(false);
        if (r === "ok") void huddle.join(roomId, name);
        else { started.current = false; want.current = false; setTalk(false); setMicErr(r === "none" ? "No microphone found." : MIC_BLOCKED); }
      });
    }
    // else the pre-connect is in flight: it lands with `want` set and opens the mic
  };
  const end = useCallback(() => {
    started.current = false; askId.current++; setAsking(false); setMicErr("");
    setTalk(false); setMuted(false);
    void setMic(false).catch(() => {});
    huddle.leave(); // explicit End stops input and playback; backend work keeps running
  }, [setMic, huddle]);
  const toggleMute = () => { const m = !muted; setMuted(m); void setMic(!m); };

  const press = () => {
    if (!room || elsewhere || down.current) return;
    if (talk && !live) { end(); return; } // a tap while it is still connecting (or asking for the mic) cancels
    down.current = { at: Date.now(), was: talk, muted };
    if (!talk) open(); // the mic opens on the press, so a push-to-talk keeps its first word
    else if (muted) { setMuted(false); void setMic(true); } // a new turn reopens only the input
  };
  const release = () => {
    const d = down.current;
    if (!d) return;
    down.current = null;
    const held = Date.now() - d.at >= HOLD_MS;
    if (held) { setMuted(true); void setMic(false); return; } // release input; the assistant may still be replying
    if (!d.was || d.muted) return; // a tap starts or resumes a hands-free conversation
    setMuted(true); void setMic(false); // tap toggles input, independently of playback or work
  };
  const cancel = () => { // undo the press: a talk it started ends, a mute it lifted comes back
    const d = down.current;
    down.current = null;
    if (d && !d.was) { setTalk(false); setMuted(false); void setMic(false); }
    else if (d?.muted) { setMuted(true); void setMic(false); }
  };
  // One listener belongs to the session, independently of which place displays an orb.
  const keyRef = useRef({ press, release, cancel });
  useEffect(() => { keyRef.current = { press, release, cancel }; });
  useEffect(() => {
    const onKey = (e: Event) => {
      const detail = (e as CustomEvent).detail, k = keyRef.current;
      if (detail === "down") k.press(); else if (detail === "cancel") k.cancel(); else k.release();
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
  useEffect(() => { ownedRoom.current = live ? connected : null; }, [live, connected]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      want.current = false;
      down.current = null;
      // Place changes keep this owner. A true scope/unmount closes only its assistant mic.
      void Promise.resolve(ownedRoom.current?.localParticipant.setMicrophoneEnabled(false)).catch(() => {});
      if (isTauri()) tellDesktop(false);
    };
  }, []);
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
    if (want.current !== connected.localParticipant.isMicrophoneEnabled) void setMic(want.current);
    else if (want.current) chime();
  }, [connected, live, setMic]);
  useEffect(() => { if (huddle.error) started.current = false; }, [huddle.error]); // a failed join never connects
  // The worker ended the talk (quiet_ended: mic closed, room warm), or left (idle_ended), or the join failed.
  const code = huddle.error?.code;
  const [seenCode, setSeenCode] = useState(code);
  if (code !== seenCode) { setSeenCode(code); if (code) { setTalk(false); setMuted(false); } }
  useEffect(() => {
    if (!code) return;
    want.current = false;
    // The worker's failure notice must close capture as well as the UI, never another room's mic.
    if (live && mine) void setMic(false).catch(() => {});
  }, [code, live, mine, setMic]);
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
  const state: Orb = mine && talk && (asking || (room && huddle.joining === room.roomId) || (live && !agent)) ? "connecting"
    : !on ? "idle"
    : agent?.status === "speaking" ? "speaking"
    : agent?.status === "working" ? (agent.task ? "working" : "thinking")
    : muted ? "muted" : needs > 0 ? "needs" : "listening";
  const caption = agent?.caption;
  const alert = micErr || (ended ? "" : err || (error ? `Couldn't reach ${name}. Try again.` : ""));
  const away = elsewhere ? `You're in a Live in ${huddle.roomName ?? "another room"}. End it to talk to ${name}.` : "";
  const label = {
    idle: `Tap to talk to ${name}`, connecting: asking ? MIC_ASK : `Connecting to ${name}…`, listening: "Listening",
    thinking: "Thinking…", working: agent?.task || "Working…", speaking: `${name} is speaking`,
    muted: "Muted", needs: `Listening · ${needs} need${needs === 1 ? "s" : ""} you on screen`,
  }[state];
  return { name, room, live, connected, micLive, on, state, label, caption, alert, ended, err, away, muted,
    press, release, cancel, toggleMute, end, tile, setHeard, elsewhere };
}

type TalkSession = ReturnType<typeof useTalkController>;
const TalkSessionContext = createContext<TalkSession | null>(null);
export const useTalkSession = () => useContext(TalkSessionContext);

/** One gesture/session owner across every place; the visible orb is only a view of it. */
export function TalkSessionProvider({ name, children }: { name: string; children: ReactNode }) {
  const session = useTalkController(name);
  return <TalkSessionContext.Provider value={session}>
    {children}
    {session.live && session.connected && <RoomContext.Provider value={session.connected}>
      <AgentHeard tile={session.tile} onHeard={session.setHeard} />
    </RoomContext.Provider>}
  </TalkSessionContext.Provider>;
}
