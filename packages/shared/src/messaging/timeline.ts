/**
 * Pure room-timeline helpers shared by the web and mobile clients: canonical
 * message merge/order, the live-run projection, day/author grouping, and the
 * room display name. No React, no platform globals.
 */
import { extractResultText } from "../chat/history";
import { accumulateStreamText } from "../live/streamText";
import type { ChatIncomingMessage, RoomRunFrame } from "../live/types";
import type { CanonicalRoomMessage, RoomMember, RoomSummary } from "./types";
import { redactToolArgs } from "./toolDisplay";

export function mergeCanonicalMessages(
  current: readonly CanonicalRoomMessage[],
  incoming: readonly CanonicalRoomMessage[],
): CanonicalRoomMessage[] {
  const byId = new Map(current.map((message) => [message.messageId, message]));
  for (const message of incoming) {
    const previous = byId.get(message.messageId);
    const content = (previous?.messageRevision ?? 0) > (message.messageRevision ?? 0) ? previous! : message;
    const reactions = (previous?.reactionRevision ?? 0) > (message.reactionRevision ?? 0) ? previous! : message;
    byId.set(message.messageId, { ...content, reactions: content.deletedAt ? {} : reactions.reactions, reactionRevision: reactions.reactionRevision });
  }
  return [...byId.values()].sort((left, right) => left.messageSeq - right.messageSeq);
}

export const lastSequence = (messages: readonly CanonicalRoomMessage[]) => messages.at(-1)?.messageSeq ?? 0;

/** A live agent run, accumulated from `room.run.frame` events. */
export interface LiveRoomRun {
  triggerMessageId?: string | null;
  roomId: string;
  agentId: string;
  runId: string;
  /** `undefined`: not known yet (a seed from an older backend); `null`: the main conversation. */
  threadRootMessageId?: string | null;
  /** The newest canonical seq this client held when the run appeared: where it sits once it ends. */
  afterSeq?: number;
  text: string;
  thinking: string;
  frames: ChatIncomingMessage[];
  state: "running" | "completed" | "failed" | "aborted";
  /** Stopped by the backend's grace timeout rather than confirmed by the runtime. */
  forcedStop?: boolean;
}

const frameSeq = (frame: ChatIncomingMessage): number | undefined => {
  const seq = (frame as { seq?: unknown }).seq;
  return typeof seq === "number" && Number.isFinite(seq) ? seq : undefined;
};

const foldText = (frames: readonly ChatIncomingMessage[], type: "chunk" | "thinking") =>
  frames.reduce((acc, frame) => (frame.type === type ? accumulateStreamText(acc, frame) : acc), "");

// ponytail: AppSync can drop a frame for good, and waiting on it would freeze the text for
// the rest of the run -- so after this many frames past a gap, render through the hole.
const GAP_PATIENCE = 16;

/** The frames a live run shows: everything up to the first `seq` gap, so a late frame fills
 * in behind the text instead of leaving a hole in it. An ended run shows all it has. */
const visibleFrames = (frames: ChatIncomingMessage[], ended: boolean): ChatIncomingMessage[] => {
  if (ended) return frames;
  let prev: number | undefined;
  for (let i = 0; i < frames.length; i++) {
    const seq = frameSeq(frames[i]);
    if (seq === undefined) continue;
    if (prev !== undefined && seq !== prev + 1) return frames.length - i > GAP_PATIENCE ? frames : frames.slice(0, i);
    prev = seq;
  }
  return frames;
};

/**
 * Fold one `room.run.frame` into its run. AppSync reorders and duplicates, so the
 * bridge numbers every frame of a run (`seq`): frames are kept in `seq` order and a
 * repeated `seq` is dropped. Frames without one -- the backend sweeper's `error` +
 * `run_ended`, an older image -- apply on arrival, after everything already held.
 * The text is refolded only when a frame lands out of order; in order it just
 * appends. A run that has ended stays ended: a straggler never revives it, and a
 * late `error` or sweeper `run_ended` never overwrites the outcome.
 */
export function applyRunFrame(previous: LiveRoomRun | undefined, payload: RoomRunFrame): LiveRoomRun {
  const frame = payload.frame;
  const seq = frameSeq(frame);
  const held = previous?.frames ?? [];
  if (seq !== undefined && previous && held.some((f) => frameSeq(f) === seq)) return previous;
  // After the end, only stragglers from before it are kept: a stopped run's card must stop
  // moving even if the agent's frames keep coming (prod 2026-09-25).
  if (previous && previous.state !== "running" && frame.type !== "run_ended") {
    const endSeq = held.map((f) => (f.type === "run_ended" ? frameSeq(f) : undefined)).find((s) => s !== undefined);
    if (seq === undefined || endSeq === undefined || seq > endSeq) return previous;
  }
  const later = seq === undefined ? -1 : held.findIndex((f) => (frameSeq(f) ?? -Infinity) > seq);
  const at = later < 0 ? held.length : later;
  const frames = [...held.slice(0, at), frame, ...held.slice(at)];
  const inOrder = at === held.length;
  const ended = previous !== undefined && previous.state !== "running";
  // Once ended, only the bridge's own (seq'd) run_ended may restate the outcome: a late
  // seq-less sweeper run_ended must not flip a run that already finished.
  const state: LiveRoomRun["state"] =
    frame.type === "run_ended" && (!ended || seq !== undefined)
      ? frame.outcome === "done" ? "completed" : frame.outcome === "aborted" ? "aborted" : "failed"
      : ended ? previous.state : frame.type === "error" ? "failed" : "running";
  const visible = visibleFrames(frames, state !== "running");
  // Appending is only safe when nothing was held back before this frame and nothing is now.
  const append = inOrder && visible === frames && visibleFrames(held, ended) === held;
  const fold = (type: "chunk" | "thinking", prev: string) =>
    !append ? foldText(visible, type) : frame.type === type ? accumulateStreamText(prev, frame) : prev;
  return {
    roomId: payload.roomId,
    agentId: payload.agentId,
    runId: payload.runId,
    triggerMessageId: previous?.triggerMessageId,
    afterSeq: previous?.afterSeq,
    threadRootMessageId: payload.threadRootMessageId ?? previous?.threadRootMessageId ?? null,
    text: fold("chunk", previous?.text ?? ""),
    thinking: fold("thinking", previous?.thinking ?? ""),
    frames,
    state,
    // The runtime's own (seq'd) run_ended confirms the stop, so it clears the caveat.
    ...(state === "aborted" && (frame.type === "run_ended" ? frame.forced && seq === undefined : previous?.forcedStop)
      ? { forcedStop: true } : {}),
  };
}

/** One tool/activity row of a run. Assignable to the web's `ToolCall`. */
export interface RunToolCall {
  id: string;
  name: string;
  args: unknown;
  result: unknown;
  status: "running" | "done" | "error";
  progressText?: string;
  kind?: "tool" | "activity";
}

export interface RoomRunView extends Omit<LiveRoomRun, "frames"> {
  tools: RunToolCall[];
  /** Follow-ups the backend steered into this run (see the `steered` frame). */
  steeredMessageIds?: string[];
}

export function frameText(blocks: unknown): string | undefined {
  return extractResultText({ content: blocks }) || undefined;
}

/**
 * Whether a run has used the shared browser: undefined when it has not, otherwise the host
 * of the site it last opened (null when no navigation named one). Drives the "Friday is
 * browsing example.com" line under a live run on web and mobile.
 */
export function browsingSite(run: Pick<RoomRunView, "tools">): string | null | undefined {
  const calls = run.tools.filter((call) => call.name.startsWith("browser_"));
  if (!calls.length) return undefined;
  const args = [...calls].reverse().find((call) => call.name === "browser_navigate")?.args as { url?: unknown } | undefined;
  try {
    return typeof args?.url === "string" ? new URL(args.url).hostname.replace(/^www\./, "") : null;
  } catch {
    return null;
  }
}

export function runView(run: LiveRoomRun): RoomRunView {
  const tools = new Map<string, RunToolCall>();
  run.frames.forEach((frame, index) => {
    if (frame.type === "tool_start") {
      const id = frame.toolCallId ?? `${frame.runId}:${frame.tool}`;
      tools.set(id, { id, name: frame.tool, args: redactToolArgs(frame.tool, frame.args ?? {}), result: null, status: "running" });
    } else if (frame.type === "tool_progress") {
      const current = tools.get(frame.toolCallId) ?? {
        id: frame.toolCallId,
        name: frame.tool,
        args: {},
        result: null,
        status: "running" as const,
      };
      tools.set(frame.toolCallId, { ...current, progressText: frameText(frame.partialResult) });
    } else if (frame.type === "tool_end" || frame.type === "tool_error") {
      const id = frame.toolCallId ?? `${frame.runId}:${frame.tool}`;
      const current = tools.get(id);
      tools.set(id, {
        id,
        name: frame.tool,
        args: current?.args ?? {},
        result: frame.result ?? frame.meta ?? null,
        status: frame.type === "tool_error" ? "error" : "done",
        progressText: current?.progressText,
      });
    } else if (frame.type === "command_output") {
      const current = tools.get(frame.toolCallId);
      if (current && frame.output) {
        tools.set(frame.toolCallId, {
          ...current,
          progressText: `${current.progressText ?? ""}${frame.output}`,
        });
      }
    } else if (frame.type === "agent_activity" && frame.activity !== "run_status") {
      const id = `${frame.runId}:activity:${frameSeq(frame) ?? index}`;
      tools.set(id, {
        id,
        name: frame.title,
        args: frame.detail ?? {},
        result: null,
        status: "done",
        kind: "activity",
      });
    }
  });
  const steeredMessageIds = run.frames.flatMap((frame) => frame.type === "steered" ? [frame.messageId] : []);
  // An ended run has no running step: after Stop the bridge sends no tool_end for the
  // interrupted call, which otherwise kept pulsing "Running" under "<agent> stopped".
  const ended = run.state !== "running";
  const settled = [...tools.values()].map((tool) =>
    ended && tool.status === "running" ? { ...tool, status: run.state === "failed" ? "error" as const : "done" as const } : tool);
  return { ...run, tools: settled, ...(steeredMessageIds.length ? { steeredMessageIds } : {}) };
}

/** What an ambient turn answers when the message needs nothing from it (the bridge posts no reply). */
const SILENT_MARKER = "[SILENT]";
/** A finished run that declined to reply: it leaves no card. */
export const declinedRun = (run: Pick<LiveRoomRun, "state" | "text">) =>
  run.state === "completed" && run.text.trim() === SILENT_MARKER;
/** The streamed text a run card shows: nothing while it is, or may still become, the marker. */
export const shownRunText = (text: string) => {
  const said = text.trim();
  return said && SILENT_MARKER.startsWith(said) ? "" : text;
};

/**
 * One "working" indicator per agent per room. An agent runs one turn at a time, so when it
 * holds several running runs (a follow-up that could not be steered queued a turn of its own,
 * or a turn in another room holds its lock) only one is doing anything: show the one that has
 * made progress (else the first) and report the others' trigger messages as waiting for it.
 * Ended runs pass through untouched.
 */
export function collapseRuns<R extends RoomRunView>(runs: readonly R[]): { runs: R[]; waiting: Map<string, string> } {
  const shown = new Map<string, R>();
  const progressed = (run: R) => !!(run.text || run.thinking || run.tools.length);
  for (const run of runs) {
    if (run.state !== "running") continue;
    const current = shown.get(run.agentId);
    if (!current || (!progressed(current) && progressed(run))) shown.set(run.agentId, run);
  }
  const waiting = new Map<string, string>();
  const kept = runs.filter((run) => {
    if (run.state !== "running" || shown.get(run.agentId) === run) return true;
    if (run.triggerMessageId) waiting.set(run.triggerMessageId, run.agentId);
    return false;
  });
  return { runs: kept, waiting };
}

/**
 * Where each run of one timeline (the main conversation, or one thread) renders. A running run
 * is the live tail, below the newest message. An ended one that was never saved as a message
 * (stopped, failed, no reply) stays where it happened -- after the newest message it was
 * started behind -- so a message sent after it ends renders below it, not hidden above it.
 */
export function placeRuns<R extends Pick<LiveRoomRun, "state" | "afterSeq">>(
  runs: readonly R[],
  messages: readonly CanonicalRoomMessage[],
): { tail: R[]; after: Map<string, R[]> } {
  const tail: R[] = [];
  const after = new Map<string, R[]>();
  const newest = lastSequence(messages);
  for (const run of runs) {
    const settledAt = run.state === "running" || run.afterSeq === undefined || run.afterSeq >= newest ? undefined : run.afterSeq;
    const anchor = settledAt === undefined ? undefined : [...messages].reverse().find((message) => message.messageSeq <= settledAt);
    if (anchor) after.set(anchor.messageId, [...(after.get(anchor.messageId) ?? []), run]);
    else tail.push(run);
  }
  return { tail, after };
}

export const GROUP_WINDOW_MS = 5 * 60 * 1000;

export type TimelineItem<M extends CanonicalRoomMessage = CanonicalRoomMessage> =
  | { kind: "date"; key: string; label: string; date: Date }
  | { kind: "new"; key: string }
  | { kind: "message"; key: string; message: M; collapsed: boolean };

const dayKey = (date: Date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;

export function dateDividerLabel(date: Date, now: Date): string {
  const startOf = (value: Date) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(date)) / 86_400_000);
  // hand-rolled on purpose: Intl.RelativeTimeFormat is not guaranteed on Hermes
  // (mobile shares this module)
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days > 1 && days < 7) return date.toLocaleDateString(undefined, { weekday: "long" });
  return date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

export function buildTimeline<M extends CanonicalRoomMessage>(
  messages: readonly M[],
  { readSeq, now = new Date() }: { readSeq: number | null; now?: Date },
): TimelineItem<M>[] {
  const items: TimelineItem<M>[] = [];
  let lastDay: string | null = null;
  let previous: M | null = null;
  let newLineDone = readSeq === null;
  for (const message of messages) {
    const created = new Date(message.createdAt);
    const valid = !Number.isNaN(created.getTime());
    let boundary = false;
    if (valid && dayKey(created) !== lastDay) {
      lastDay = dayKey(created);
      items.push({ kind: "date", key: `date:${lastDay}`, label: dateDividerLabel(created, now), date: created });
      boundary = true;
    }
    if (!newLineDone && readSeq !== null && message.messageSeq > readSeq) {
      items.push({ kind: "new", key: "new" });
      newLineDone = true;
      boundary = true;
    }
    const collapsed = !boundary && previous !== null && !message.deletedAt && !previous.deletedAt
      && previous.author.kind === message.author.kind && previous.author.id === message.author.id
      && valid && created.getTime() - new Date(previous.createdAt).getTime() <= GROUP_WINDOW_MS;
    items.push({ kind: "message", key: `message:${message.messageId}`, message, collapsed });
    previous = message;
  }
  return items;
}

export function roomDisplayName(
  room: Pick<RoomSummary, "kind" | "name"> | undefined,
  participants: readonly Pick<RoomMember, "displayName">[],
): string {
  if (!room) return "Chat";
  // A channel's `#` is the surface's to draw (the phone's tile and composer,
  // the web's Live label), so a name saved with one ("#to-do-updates") read as
  // "##to-do-updates" there.
  if (room.name) return (room.kind === "channel" && room.name.replace(/^#+\s*/, "")) || room.name;
  const names = participants.map((participant) => participant.displayName);
  if (room.kind === "dm") return names[0] ?? "Direct message";
  if (room.kind === "group-dm") return names.length ? names.join(" and ") : "Group chat";
  return "Untitled project";
}
