/**
 * Pure room-timeline helpers shared by the web and mobile clients: canonical
 * message merge/order, the live-run projection, day/author grouping, and the
 * room display name. No React, no platform globals.
 */
import { extractResultText } from "../chat/history";
import type { ChatIncomingMessage } from "../live/types";
import type { CanonicalRoomMessage, RoomMember, RoomSummary } from "./types";

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
  roomId: string;
  agentId: string;
  runId: string;
  threadRootMessageId?: string | null;
  text: string;
  thinking: string;
  frames: ChatIncomingMessage[];
  state: "running" | "completed" | "failed" | "aborted";
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
}

export function frameText(blocks: unknown): string | undefined {
  return extractResultText({ content: blocks }) || undefined;
}

export function runView(run: LiveRoomRun): RoomRunView {
  const tools = new Map<string, RunToolCall>();
  run.frames.forEach((frame, index) => {
    if (frame.type === "tool_start") {
      const id = frame.toolCallId ?? `${frame.runId}:${frame.tool}`;
      tools.set(id, { id, name: frame.tool, args: frame.args ?? {}, result: null, status: "running" });
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
      const id = `${frame.runId}:activity:${index}`;
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
  return { ...run, tools: [...tools.values()] };
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
  if (!room) return "Conversation";
  if (room.name) return room.name;
  const names = participants.map((participant) => participant.displayName);
  if (room.kind === "dm") return names[0] ?? "Direct message";
  if (room.kind === "group-dm") return names.length ? names.join(" and ") : "Group chat";
  return "Untitled channel";
}
