import type { ApiMethods } from "../api/client";
import type { LiveEvent } from "../live/types";
import { takeSentences } from "./sentences";

export interface VoiceChannel { send(data: string): void; onmessage: ((ev: { data: string }) => void) | null; }
export interface VoicePeer { offerSdp: string; channel: VoiceChannel; accept(answerSdp: string): Promise<void>; close(): void; /** Set by the session; the peer calls it when the connection is lost. */ onFailed?: (() => void) | null; levels?(): { mic: number; out: number }; /** n speech-range frequency bands per side, each 0..1. */ bands?(n: number): { mic: number[]; out: number[] }; setMuted?(muted: boolean): void; }
export interface VoiceRtc { open(): Promise<VoicePeer>; }
export type VoiceCallState = "connecting" | "live" | "ending" | "ended" | "failed";
export type VoiceTarget = { agentId: string; agentName: string } | { roomId: string };
export interface VoiceCallView {
  state: VoiceCallState; deadline: number | null; error: string | null;
  kind: "dm" | "room" | null; voiceAgentId: string | null; voiceName: string | null; team: { id: string; name: string }[];
  working: { agentId: string; label: string }[];
  /** Delegations with no frame yet (drives "Thinking..."). */
  pending: number;
  captions: { role: "you" | "agent"; text: string }[];
  muted: boolean;
}

const TRANSCRIPT_CHARS = 6000;
const APPEND_CHARS = 1400; // OpenAI caps each append at 500 tokens
const CLOSE_WAIT_MS = 15_000;
const WARN_BEFORE_MS = 60_000;
const UTTERANCE_CHARS = 4000;
// ponytail: the bridge publishes deltas in batches of 5 (FRAME_BATCH, hermes bridge turn.py) and AppSync can swap whole
// publishes, so deltas are held 10 back (2 batches). Reordering beyond that applies as it arrives; raise this with FRAME_BATCH.
const REORDER_WINDOW = 10;
const CAPTION_MS = 100; // <= 10 caption updates/s

interface Turn { order: number; turnId: string | null; agentId: string | null; framed: boolean; buffered: Array<{ turnId: string; frame: Frame }>; pending: Array<{ seq: number; frame: Frame }>; ended: boolean; resetSeq: number; auto: number; delegationId: string; text: string; spoken: string; label: string | null; }

type Frame = { type: string; [k: string]: unknown };

export class VoiceCallSession {
  private closed = false;
  private turnSeq = 0; // per call, strictly increasing; the backend orders turns by it
  private boundary = false; // a delegation closed the last utterance; the next words start a new line
  private peer: VoicePeer | null = null;
  private callId: string | null = null;
  private lines: Array<{ role: "User" | "Voice assistant"; text: string }> = [];
  private turns = new Map<string, Turn>(); // by delegation id
  private latestByAgent = new Map<string, string>(); // agentId -> delegation id
  private names = new Map<string, string>();
  private lastCaption = -Infinity;
  private captionTimer: unknown = null;
  private listeners = new Set<(view: VoiceCallView) => void>();
  private view: VoiceCallView = { state: "connecting", deadline: null, error: null, kind: null, voiceAgentId: null, voiceName: null, team: [], working: [], pending: 0, captions: [], muted: false };
  private offEvents: (() => void) | null = null;
  private timers: unknown[] = [];

  constructor(private deps: {
    api: ApiMethods; rtc: VoiceRtc; onEvent: (h: (e: LiveEvent) => void) => () => void;
    now?: () => number; setTimer?: typeof setTimeout; clearTimer?: typeof clearTimeout;
  }) {}

  subscribe(listener: (view: VoiceCallView) => void): () => void {
    this.listeners.add(listener);
    listener(this.view);
    return () => this.listeners.delete(listener);
  }

  async start(target: VoiceTarget): Promise<void> {
    try {
      this.offEvents = this.deps.onEvent((e) => this.onLive(e));
      const peer = await this.deps.rtc.open();
      if (this.closed) { peer.close(); return; }
      this.peer = peer;
      peer.onFailed = () => this.lost();
      peer.channel.onmessage = (ev) => {
        try { this.onOai(JSON.parse(ev.data)); } catch { /* ignore malformed data-channel message */ }
      };
      const res = (await this.deps.api.post("/calls", "roomId" in target ? { roomId: target.roomId, sdp: peer.offerSdp } : { agentId: target.agentId, agentName: target.agentName, sdp: peer.offerSdp })) as { callId: string; sdp: string; deadline: number; kind: "dm" | "room"; voiceAgentId?: string; voiceName: string; team: { id: string; name: string }[] };
      this.names = new Map(res.team.map((t) => [t.id, t.name]));
      this.callId = res.callId;
      if (this.closed) return this.abandon();
      await peer.accept(res.sdp);
      if (this.closed) return this.abandon();
      this.set({ state: "live", deadline: res.deadline * 1000, kind: res.kind, voiceAgentId: res.voiceAgentId ?? null, voiceName: res.voiceName, team: res.team });
      const warnIn = res.deadline * 1000 - WARN_BEFORE_MS - this.now();
      if (warnIn > 0) this.timers.push(this.timer(() => this.oai({ type: "session.instructions.append", content: "Tell the user there is about one minute left on this call." }), warnIn));
    } catch (error) {
      this.abandon();
      if (this.closed) return; // the user hung up; nothing to report
      this.view = { ...this.view, state: "failed", error: errorCode(error) };
      this.teardown();
    }
  }

  /** Best-effort end of a backend call that start() opened but the user no longer wants. */
  private abandon(): void {
    if (this.callId) void Promise.resolve(this.deps.api.post(`/calls/${this.callId}/end`, {})).catch(() => {});
    this.callId = null;
  }

  async hangUp(): Promise<void> {
    if (!this.callId || this.view.state !== "live") return this.teardown();
    this.set({ state: "ending" });
    this.timers.push(this.timer(() => this.teardown(), CLOSE_WAIT_MS));
    try {
      await this.deps.api.post(`/calls/${this.callId}/end`, {});
    } catch {
      // teardown below still runs; the backend sweeper closes a call we failed to end
    } finally {
      this.teardown();
    }
  }

  private onOai(event: { type?: string; delta?: string; delegation?: { id: string } }): void {
    if (event.type === "session.input_transcript.delta" && event.delta) this.note("User", event.delta);
    else if (event.type === "session.output_transcript.delta" && event.delta) this.note("Voice assistant", event.delta);
    else if (event.type === "session.delegation.created" && event.delegation) void this.delegate(event.delegation.id);
    else if (event.type === "session.closed") this.lost(false);
  }

  /** The call ended without the user hanging up: end it on the backend so it stops billing. */
  private lost(dropped = true): void {
    if (this.closed || this.view.state === "ending") return;
    if (this.view.state === "live") this.abandon();
    if (dropped) this.view = { ...this.view, state: "failed", error: "connection_lost" };
    this.teardown();
  }

  private note(role: "User" | "Voice assistant", delta: string): void {
    const last = this.lines[this.lines.length - 1];
    if (last?.role === role && !this.boundary) last.text += delta;
    else { this.lines.push({ role, text: delta }); this.boundary = false; }
    this.captions();
  }

  private captions(): void {
    if (this.captionTimer !== null) return;
    const wait = this.lastCaption + CAPTION_MS - this.now();
    const emit = () => {
      this.captionTimer = null;
      this.lastCaption = this.now();
      this.set({ captions: this.lines.slice(-2).map((l) => ({ role: l.role === "User" ? "you" : "agent", text: l.text.trim() })) });
    };
    if (wait <= 0) return emit();
    this.captionTimer = this.timer(emit, wait);
  }

  setMuted(muted: boolean): void {
    this.peer?.setMuted?.(muted);
    this.oai({ type: muted ? "session.input_audio.mute" : "session.input_audio.unmute" });
    this.set({ muted });
  }

  levels(): { mic: number; out: number } { return this.peer?.levels?.() ?? { mic: 0, out: 0 }; }

  bands(n: number): { mic: number[]; out: number[] } { return this.peer?.bands?.(n) ?? { mic: new Array<number>(n).fill(0), out: new Array<number>(n).fill(0) }; }

  private nameOf(turn: Turn): string { return (turn.agentId && this.names.get(turn.agentId)) || this.view.voiceName || "the team"; }

  private emitWorking(): void {
    const all = [...this.turns.values()];
    this.set({
      working: all.filter((t) => t.label && !t.ended).map((t) => ({ agentId: t.agentId!, label: t.label! })),
      pending: all.filter((t) => !t.framed).length,
    });
  }

  private async delegate(delegationId: string): Promise<void> {
    if (!this.callId) return;
    const utterance = [...this.lines].reverse().find((l) => l.role === "User")?.text.trim().slice(-UTTERANCE_CHARS) ?? "";
    const transcript = this.lines.map((l) => `${l.role}: ${l.text.trim()}`).join("\n").slice(-TRANSCRIPT_CHARS);
    const order = ++this.turnSeq;
    const turn: Turn = { order, turnId: null, agentId: null, framed: false, buffered: [], pending: [], ended: false, resetSeq: -1, auto: 0, delegationId, text: "", spoken: "", label: null };
    this.boundary = true;
    this.turns.set(delegationId, turn);
    this.emitWorking();
    try {
      const res = (await this.deps.api.post(`/calls/${this.callId}/turns`, { utterance: utterance || "(no words heard)", transcript, seq: order })) as { turnId: string; agentId: string };
      turn.turnId = res.turnId;
      turn.agentId = res.agentId;
      if (this.closed) return;
      // Supersession follows delegation order, not the order POSTs resolve.
      const prev = this.latestByAgent.get(res.agentId);
      const prevTurn = prev ? this.turns.get(prev) : undefined;
      if (prevTurn && prev !== delegationId && prevTurn.order > order) { this.turns.delete(delegationId); this.emitWorking(); return; }
      if (prev && prev !== delegationId) this.turns.delete(prev); // the same agent's older turn: its frames are now ignored
      this.latestByAgent.set(res.agentId, delegationId);
      // Room calls: name the teammate now, so the voice can attribute a result that arrives with no tool frame.
      if (this.view.kind === "room") this.oai({ type: "session.thinking.append", delegation_id: delegationId, content: `${this.nameOf(turn)} is on it. Not done yet.` });
      const buffered = turn.buffered;
      turn.buffered = [];
      if (this.turns.get(delegationId) === turn) for (const b of buffered) if (b.turnId === res.turnId) this.applyFrame(turn, b.frame);
    } catch (error) {
      // turn_conflict: a newer delegation from this session won and will speak
      if (errorDetailCode(error) !== "turn_conflict") this.commentary(turn, "Sorry, I couldn't reach the team for that.");
      this.turns.delete(delegationId);
    }
    if (!this.closed) this.emitWorking();
  }

  private onLive(event: LiveEvent): void {
    if (event.type !== "call.turn.frame") return;
    const { callId, turnId, frame } = event.payload;
    if (callId !== this.callId) return;
    const turn = [...this.turns.values()].find((t) => t.turnId === turnId);
    if (turn) return this.applyFrame(turn, frame);
    // Not resolved yet: hold it on every unresolved turn; each replays only frames with its own turnId.
    for (const t of this.turns.values()) if (t.turnId === null) t.buffered.push({ turnId, frame });
  }

  /** Bridge frames carry a per-run `seq`; AppSync does not keep publish order, so deltas are re-sorted here. */
  private applyFrame(turn: Turn, frame: Frame): void {
    if (!turn.framed) { turn.framed = true; this.emitWorking(); }
    const seq = typeof frame.seq === "number" ? frame.seq : ++turn.auto;
    if (frame.type === "chunk" && frame.append) {
      if (seq <= turn.resetSeq) return; // narration from before a tool_start
      turn.pending.push({ seq, frame });
      turn.pending.sort((x, y) => x.seq - y.seq);
      while (turn.pending.length > REORDER_WINDOW) this.handle(turn, turn.pending.shift()!.frame);
      return;
    }
    // Any other frame: everything ordered before it is applied first.
    while (turn.pending.length && (frame.type !== "tool_start" || turn.pending[0].seq < seq)) this.handle(turn, turn.pending.shift()!.frame);
    if (frame.type === "tool_start" || frame.type === "chunk") turn.resetSeq = Math.max(turn.resetSeq, seq);
    this.handle(turn, frame);
  }

  private handle(turn: Turn, frame: Frame): void {
    if (frame.type === "tool_start" && typeof frame.tool === "string") {
      turn.text = ""; // narration so far was already spoken; the next text box is a new answer
      turn.spoken = "";
      if (frame.tool === turn.label) return;
      turn.label = frame.tool;
      this.emitWorking();
      this.oai({ type: "session.thinking.append", delegation_id: turn.delegationId, content: `${this.nameOf(turn)} is working: ${frame.tool}. Not done yet.` });
    } else if (frame.type === "chunk" && typeof frame.content === "string") {
      if (frame.append) turn.text += frame.content;
      else if (frame.content.startsWith(turn.spoken)) turn.text = frame.content;
      else { // deltas were lost: speak the whole answer only if nothing was spoken since the reset
        const nothingSpoken = !turn.spoken;
        turn.text = frame.content;
        turn.spoken = nothingSpoken ? "" : frame.content;
      }
      this.speakReady(turn, turn.ended); // a closing chunk that lands after run_ended is final
    } else if (frame.type === "error" && typeof frame.message === "string") {
      turn.text = turn.spoken = "(apology)"; // counts as spoken so run_ended stays quiet
      this.commentary(turn, `Sorry, I couldn't reach ${this.nameOf(turn)} for that.`);
    } else if (frame.type === "run_ended") {
      turn.ended = true;
      this.speakReady(turn, true);
      if (!turn.spoken) this.oai({ type: "session.thinking.append", delegation_id: turn.delegationId, content: `${this.nameOf(turn)} finished that request without a spoken result.` });
      this.emitWorking();
    }
  }

  private speakReady(turn: Turn, final: boolean): void {
    const pending = turn.text.slice(turn.spoken.length);
    const { ready, rest } = final ? { ready: pending.trim(), rest: "" } : takeSentences(pending);
    if (!ready) return;
    turn.spoken = turn.text.slice(0, turn.text.length - rest.length);
    for (let i = 0; i < ready.length; i += APPEND_CHARS) this.commentary(turn, ready.slice(i, i + APPEND_CHARS));
  }

  private commentary(turn: Turn, content: string): void {
    this.oai({ type: "session.commentary.append", delegation_id: turn.delegationId, content });
  }

  private oai(event: Record<string, unknown>): void {
    this.peer?.channel.send(JSON.stringify({ event_id: `evt_${Math.random().toString(36).slice(2)}`, ...event }));
  }

  private teardown(): void {
    this.timers.forEach((t) => (this.deps.clearTimer ?? clearTimeout)(t as never));
    this.timers = [];
    if (this.captionTimer !== null) (this.deps.clearTimer ?? clearTimeout)(this.captionTimer as never);
    this.captionTimer = null;
    this.offEvents?.();
    this.offEvents = null;
    this.closed = true;
    this.peer?.close();
    this.peer = null;
    if (this.view.state === "failed") this.set({}); else this.set({ state: "ended", working: [], pending: 0 });
  }

  private set(patch: Partial<VoiceCallView>): void {
    this.view = { ...this.view, ...patch };
    this.listeners.forEach((l) => l(this.view));
  }

  private now(): number { return (this.deps.now ?? Date.now)(); }
  private timer(fn: () => void, ms: number): unknown { return (this.deps.setTimer ?? setTimeout)(fn, ms); }
}

function errorDetailCode(error: unknown): string | undefined {
  return (error as { body?: { detail?: { code?: string } } })?.body?.detail?.code;
}

function errorCode(error: unknown): string {
  const detail = (error as { body?: { detail?: { code?: string; reason?: string } } })?.body?.detail;
  return detail?.reason ?? detail?.code ?? "call_failed";
}
