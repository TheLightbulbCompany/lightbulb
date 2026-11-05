/**
 * The frozen live-event contract (architecture spec §8) plus the OpenClaw
 * run/tool frame union that rides inside `room.run.frame`.
 *
 * Every public field is camelCase. `ChatIncomingMessage` is the pre-AppSync
 * gateway frame union moved here unchanged except for the two transport
 * normalizations `agent_id -> agentId` and `session_id -> sessionId`, so the
 * existing stream/tool/run reducers keep working on the same variants.
 */

import type { CanonicalRoomMessage, MessageAttachment, RoomLastMessage } from "../messaging/types";
import { MESSAGE_ATTACHMENT_MAX_BYTES } from "../messaging/types";
import { PERSON_COLORS, type PersonColor } from "../messaging/personColor";

/** OpenClaw tool result content blocks — text blocks or image refs. Image
 *  data is replaced with `{ bytes, omitted: true }` by OpenClaw's sanitizer. */
export type ToolResultBlock = { type: string; text?: string; bytes?: number; omitted?: boolean };

export type ChatIncomingMessage =
  // Streaming text. `append: true` means `content` is an incremental delta to
  // append to the run's accumulated text; absent means `content` is the full
  // text of the CURRENT assistant message only (segment-scoped after a tool
  // call — see ./streamText).
  | { type: "chunk"; content: string; append?: boolean; agentId?: string; runId: string; sessionId?: string }
  | { type: "thinking"; content: string; append?: boolean; agentId?: string; runId: string; sessionId?: string }
  | { type: "done"; agentId?: string; runId: string; sessionId?: string }
  | { type: "error"; message: string; code?: string; agentId?: string; runId?: string; sessionId?: string }
  // Authoritative "run truly ended" frame — ADDITIONAL to done/error, exactly
  // once per run. The chat queue drains off this signal.
  // `forced`: the backend closed a Stop the runtime never confirmed; the agent may still be
  // finishing its current step.
  | { type: "run_ended"; runId: string; outcome: "done" | "error" | "aborted"; forced?: boolean; agentId?: string; sessionId?: string }
  // Server-truth busy signal derived from OpenClaw's sessions.changed.
  | { type: "run_active"; active: boolean; agentId?: string; sessionId?: string }
  // The backend delivered a human follow-up (canonical message `messageId`) into this live
  // run instead of starting a second one: the "<agent> saw this" receipt.
  | { type: "steered"; runId: string; messageId: string; agentId?: string }
  | { type: "heartbeat" }
  | {
      type: "tool_start";
      tool: string;
      toolCallId?: string;
      args?: Record<string, unknown>;
      agentId?: string;
      runId: string;
      sessionId?: string;
    }
  | {
      type: "tool_end";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agentId?: string;
      runId: string;
      sessionId?: string;
    }
  | {
      type: "tool_error";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agentId?: string;
      runId: string;
      sessionId?: string;
    }
  // Live incremental tool output (OpenClaw tool stream phase:"update").
  | {
      type: "tool_progress";
      tool: string;
      toolCallId: string;
      partialResult?: ToolResultBlock[];
      agentId?: string;
      runId: string;
      sessionId?: string;
    }
  // Live command stdout/stderr streamed mid-exec.
  | {
      type: "command_output";
      toolCallId: string;
      output?: string;
      agentId?: string;
      runId: string;
      sessionId?: string;
    }
  // Low-noise agent-activity surfaces folded into one pill. `run_status` and
  // `compaction` ride the same envelope but describe the run's state.
  | {
      type: "agent_activity";
      activity: "plan" | "patch" | "item" | "run_status" | "compaction";
      title: string;
      detail?: Record<string, unknown>;
      agentId?: string;
      runId: string;
      sessionId?: string;
    }
  | { type: "update_available" };

export const CHAT_FRAME_TYPES: ReadonlySet<string> = new Set<ChatIncomingMessage["type"]>([
  "chunk",
  "thinking",
  "done",
  "error",
  "run_ended",
  "run_active",
  "steered",
  "heartbeat",
  "tool_start",
  "tool_end",
  "tool_error",
  "tool_progress",
  "command_output",
  "agent_activity",
  "update_available",
]);

// =============================================================================
// Live events (spec §8) — the exact founder-release registry.
// =============================================================================

export type LiveEventType =
  | "room.message.reactions.changed"
  | "room.message.created"
  | "room.message.updated"
  | "room.message.available"
  | "room.typing.changed"
  | "room.activity.changed"
  | "room.run.frame"
  | "room.browser.frame"
  | "room.subagent.event"
  | "room.metadata.changed"
  | "workspace.sessions.changed"
  | "workspace.access.removed"
  | "workspace.session.attention"
  | "workspace.session.screen"
  | "workspace.session.typing"
  | "room.member.updated"
  | "room.subscription.rotated"
  | "room.access.removed"
  | "room.read.changed"
  | "room.delivery.changed"
  | "room.huddle.changed"
  | "runtime.operation.changed"
  | "runtime.resource.invalidated"
  | "paperclip.event"
  | "digest.ready"
  | "call.turn.frame"
  | "task.changed";

export const LIVE_EVENT_TYPES: ReadonlySet<string> = new Set<LiveEventType>([
  "room.message.reactions.changed",
  "room.message.created",
  "room.message.updated",
  "room.message.available",
  "room.typing.changed",
  "room.activity.changed",
  "room.run.frame",
  "room.browser.frame",
  "room.subagent.event",
  "room.metadata.changed",
  "workspace.sessions.changed",
  "workspace.access.removed",
  "workspace.session.attention",
  "workspace.session.screen",
  "workspace.session.typing",
  "room.member.updated",
  "room.subscription.rotated",
  "room.access.removed",
  "room.read.changed",
  "room.delivery.changed",
  "room.huddle.changed",
  "runtime.operation.changed",
  "runtime.resource.invalidated",
  "paperclip.event",
  "digest.ready",
  "call.turn.frame",
  "task.changed",
]);

export interface TaskChanged { taskId: string; part: string; version: number }

/** One agent-turn frame of a voice call, published by the bridge (no roomId). */
export interface CallTurnFrame {
  callId: string;
  turnId: string;
  agentId: string;
  frame: { type: string; [key: string]: unknown };
}

/** The daily "needs you" digest. `push` is the server-rendered banner (the same
 * text as the phone push and the email): show it verbatim, never compose. */
export type DigestReady = {
  countsByKind: { approval: number; assigned_issue: number; mention: number };
  total: number;
  activityUrl: string;
  push: { title: string; subtitle?: string; body: string; data: { url: string } };
};

export interface LiveEventEnvelope<T extends LiveEventType, P> {
  schemaVersion: 1;
  eventId: string;
  type: T;
  occurredAt: string;
  roomId?: string;
  messageSeq?: number;
  roomRevision?: number;
  payload: P;
}

/** The banner the projector rendered for this reader: the same text the phone
 * push carries, present only when it notifies an unmuted reader. */
export type MessagePush = {
  /** "Alice · #general": who · where. */
  title: string;
  /** Only from a projector older than the two-line banner. */
  subtitle?: string;
  body: string;
  threadId: string;
  data: { roomId: string; messageId: string; messageSeq: number; threadRootMessageId: string | null; roomKind?: string; runId?: string };
};
/** One of YOUR shared sessions is waiting for you or finished a long turn. `push` is the
 * phone's banner, verbatim: show it, never compose. */
export type SessionAttention = {
  sessionId: string;
  /** join_request: a teammate asked to join one of your sessions. join_allowed / join_declined:
   *  the owner answered YOUR request to join theirs. */
  /** attention: a job left a hand-off note (sent only to someone who chose a banner for it). */
  state: "waiting" | "finished" | "attention" | "join_request" | "join_allowed" | "join_declined";
  push: { title: string; body: string; data: { source: "session"; sessionId: string } };
  /** The owner's choice for this kind: only "banner" shows one. Absent (older server) = banner. */
  level?: "off" | "badge" | "banner";
  sound?: boolean;
  /** No banner while Lightbulb is the focused app. */
  onlyUnfocused?: boolean;
  /** A held session send: the session's Mac posts its own notification, with Allow and Decline. */
  held?: boolean;
  /** For a held send: the device id of the Mac that hosts the session. */
  hostDeviceId?: string;
};
const SCREEN_KINDS = new Set<string>(["snapshot", "output", "history"]);
const SESSION_ATTENTION_STATES = new Set<string>(["waiting", "finished", "attention", "join_request", "join_allowed", "join_declined"]);
/** One piece of a running session's live terminal, for a person watching it (spec §5.2). `epoch`
 *  is one relay run on the Mac and `seq` orders frames within it; a "snapshot" redraws the
 *  screen, "history" is the holder's recent output sent before a snapshot, "ping" (no data, no
 *  seq of its own) proves a quiet stream alive. `data` is base64 terminal output. Nothing stores it. */
export type SessionScreen = {
  sessionId: string; generation: string; epoch: number; seq: number;
  cols: number; rows: number; kind: "snapshot" | "output" | "history" | "ping"; data: string;
  /** A snapshot too big for one frame: it and the next `parts - 1` seqs (kind "output") are one picture, written in one write. Old Macs omit it. */
  parts?: number;
  /** The teammate who typed last, shown on the cursor for `forMs` (S8). Absent: the plain cursor.
   *  Members get their `userId`; a link viewer gets only their `personColor` as `color`, never the id. */
  typist?: { userId?: string; color?: PersonColor; name: string; forMs: number };
};
/** A shared session's browser tab as a 480 px JPEG, on the session's frames
 *  channel: `data` is base64, `w`x`h` the tab's CSS size (viewer input is in those pixels), `seq`
 *  orders frames. Frames flow only while someone has the Browser tab open; nothing stores them. */
export type SessionBrowserFrame = { sessionId: string; kind: "image"; data: string; w: number; h: number; seq: number };
/** An admitted person started (or is still) typing into this owner's live session terminal. */
export type SessionTyping = { sessionId: string; userId: string };
export type RoomTypingChanged = { roomId: string; userId: string; displayName: string; expiresAt: string };
/** A member's typing/working/idle hint on this room, delivered on the member's own user
 *  channel. `expiresAt` is authoritative for BOTH "typing" and "working" (a "working" hint
 *  is refreshed by every run heartbeat, so a lost/reordered "idle" self-heals); "idle" carries
 *  no expiry and clears the entry immediately. */
export type RoomActivityChanged = {
  roomId: string;
  actor: { kind: "human" | "agent"; id: string; displayName: string };
  state: "typing" | "working" | "idle";
  expiresAt?: string;
};
export type RoomRunFrame = { roomId: string; agentId: string; runId: string; threadRootMessageId?: string | null; frame: ChatIncomingMessage };
export type RoomSubagentEvent = {
  roomId: string;
  agentId: string;
  events: { type: string; seq: number; payload: Record<string, unknown> }[];
};
/** A live JPEG of a run's own browser tab, or of a Mac-cast session, for people who cannot
 * have the one live view. `w`x`h` is the tab/screen's CSS size: clicks are sent in it. `tier`
 * distinguishes a Mac cast's sharp view frames (its own `/frames` sibling channel) from its
 * low-rate thumbnail (the room channel); absent means the legacy AgentCore browser-tab frame. */
export type RoomBrowserFrame = { roomId: string; agentId: string; runId: string; seq: number; jpegBase64: string; w: number; h: number; tier?: "view" | "thumb" };
export type RoomMetadataChanged = { roomId: string; roomRevision: number };
export type RoomMemberUpdated = { roomId: string };
export type RoomSubscriptionRotated = { roomId: string; deliveryKey: string; roomRevision: number };
export type RoomAccessRemoved = { roomId: string; roomRevision: number };
/** A reader's receipt: a human's read cursor moved, or an agent's run started on a
 *  message ("read" for an agent means its run began -- see `messaging.py::handle_run_event`). */
export type RoomReadChanged =
  | { roomId: string; reader: { kind: "human"; id: string }; lastReadSeq: number }
  | { roomId: string; reader: { kind: "agent"; id: string }; messageId: string };
/** Who is in the room's huddle now (humans only; the CEO worker is not listed). */
export interface RoomHuddleChanged { roomId: string; userIds: string[] }
export type RoomDeliveryChanged = { roomId: string; agentId: string; lastDeliveredSeq: number };
export type RuntimeOperationChanged = {
  operationId: string;
  state: "queued" | "running" | "succeeded" | "failed";
  updatedAt: string;
  result: unknown | null;
  error: { code: string; message: string; retryable: boolean } | null;
};
export type RuntimeResource = "agents" | "config" | "models" | "skills" | "tools" | "health" | "onboarding" | "activity" | "mail" | "apps";
export type RuntimeResourceInvalidated = { resource: RuntimeResource; resourceId?: string; revision?: number };
export type PaperclipEventName =
  | "teams.activity.logged"
  | "teams.agent.status"
  | "teams.heartbeat.run.queued"
  | "teams.heartbeat.run.status"
  | "teams.heartbeat.run.event"
  | "teams.heartbeat.run.log"
  | "teams.external_object.updated"
  | "teams.stream.resumed";
export type PaperclipEvent = { eventName: PaperclipEventName; payload: unknown };

export type LiveEvent =
  | LiveEventEnvelope<"room.message.created", CanonicalRoomMessage>
  | LiveEventEnvelope<"room.message.updated", { roomId: string; messageId: string; messageSeq: number }>
  | LiveEventEnvelope<"room.message.available", {
      roomId: string; messageId: string; messageSeq: number; threadRootMessageId: string | null; notify: boolean;
      /** This message's preview; `previewOnly` carries the room's replacement preview (or null) instead. */
      lastMessage?: RoomLastMessage | null;
      /** Humans @mentioned in this message, at most 32. */
      mentionedUserIds?: string[];
      /** Authenticated human accountable for a delegated/system-authored message. */
      accountableHumanId?: string;
      /** An edit/delete of the room's current preview: update the preview, leave counts alone. */
      previewOnly?: true;
      /** The phone's banner for this reader, present only when it notifies an unmuted reader. */
      push?: MessagePush;
    }>
  | LiveEventEnvelope<"room.message.reactions.changed", CanonicalRoomMessage>
  | LiveEventEnvelope<"room.typing.changed", RoomTypingChanged>
  | LiveEventEnvelope<"room.activity.changed", RoomActivityChanged>
  | LiveEventEnvelope<"room.run.frame", RoomRunFrame>
  | LiveEventEnvelope<"room.browser.frame", RoomBrowserFrame>
  | LiveEventEnvelope<"room.subagent.event", RoomSubagentEvent>
  | LiveEventEnvelope<"room.metadata.changed", RoomMetadataChanged>
  | LiveEventEnvelope<"workspace.sessions.changed", { humanUserId: string; transcript?: true; sessionId?: string }>
  /** You were removed from this workspace: drop its live subscriptions (user channel only). */
  | LiveEventEnvelope<"workspace.access.removed", { workspaceId: string }>
  | LiveEventEnvelope<"workspace.session.attention", SessionAttention>
  | LiveEventEnvelope<"workspace.session.screen", SessionScreen | SessionBrowserFrame>
  | LiveEventEnvelope<"workspace.session.typing", SessionTyping>
  | LiveEventEnvelope<"room.member.updated", RoomMemberUpdated>
  | LiveEventEnvelope<"room.subscription.rotated", RoomSubscriptionRotated>
  | LiveEventEnvelope<"room.access.removed", RoomAccessRemoved>
  | LiveEventEnvelope<"room.read.changed", RoomReadChanged>
  | LiveEventEnvelope<"room.delivery.changed", RoomDeliveryChanged>
  | LiveEventEnvelope<"room.huddle.changed", RoomHuddleChanged>
  | LiveEventEnvelope<"runtime.operation.changed", RuntimeOperationChanged>
  | LiveEventEnvelope<"runtime.resource.invalidated", RuntimeResourceInvalidated>
  | LiveEventEnvelope<"paperclip.event", PaperclipEvent>
  | LiveEventEnvelope<"digest.ready", DigestReady>
  | LiveEventEnvelope<"call.turn.frame", CallTurnFrame>
  /** A task row changed (ids only; refetch through the API). Rides `/ws/{ws}/tasks`. */
  | LiveEventEnvelope<"task.changed", TaskChanged>;

export type LiveEventOf<T extends LiveEventType> = Extract<LiveEvent, { type: T }>;

const RUNTIME_RESOURCES: ReadonlySet<string> = new Set<RuntimeResource>([
  "agents", "config", "models", "skills", "tools", "health", "onboarding", "activity", "mail", "apps",
]);
const OPERATION_STATES: ReadonlySet<string> = new Set(["queued", "running", "succeeded", "failed"]);
const PAPERCLIP_EVENT_NAMES: ReadonlySet<string> = new Set<PaperclipEventName>([
  "teams.activity.logged",
  "teams.agent.status",
  "teams.heartbeat.run.queued",
  "teams.heartbeat.run.status",
  "teams.heartbeat.run.event",
  "teams.heartbeat.run.log",
  "teams.external_object.updated",
  "teams.stream.resumed",
]);

const isObj = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const str = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const text = (value: unknown): value is string => typeof value === "string";
const num = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const nullableString = (value: unknown): value is string | null => value === null || typeof value === "string";
const memberKind = (value: unknown) => value === "human" || value === "agent";
const authorKind = (value: unknown) => memberKind(value) || value === "system";
const memberRef = (value: unknown) => isObj(value) && memberKind(value.kind) && str(value.id);

function chatFrameValid(value: unknown): value is ChatIncomingMessage {
  if (!isObj(value) || !str(value.type) || !CHAT_FRAME_TYPES.has(value.type)) return false;
  const run = () => str(value.runId);
  switch (value.type) {
    case "chunk":
    case "thinking":
      return text(value.content) && run();
    case "done":
      return run();
    case "error":
      return str(value.message);
    case "run_ended":
      return run() && (value.outcome === "done" || value.outcome === "error" || value.outcome === "aborted");
    case "run_active":
      return typeof value.active === "boolean";
    case "steered":
      return run() && str(value.messageId);
    case "heartbeat":
    case "update_available":
      return true;
    case "tool_start":
      return str(value.tool) && run();
    case "tool_end":
    case "tool_error":
      return str(value.tool) && run() && (value.result === undefined || Array.isArray(value.result));
    case "tool_progress":
      return str(value.tool) && str(value.toolCallId) && run();
    case "command_output":
      return str(value.toolCallId) && run();
    case "agent_activity":
      return ["plan", "patch", "item", "run_status", "compaction"].includes(String(value.activity)) && str(value.title) && run();
    default:
      return false;
  }
}

function canonicalMessageValid(value: unknown): value is CanonicalRoomMessage {
  if (isObj(value) && value.messageRevision !== undefined && (!Number.isInteger(value.messageRevision) || (value.messageRevision as number) < 0)) return false;
  if (isObj(value) && ((value.editedAt !== undefined && !nullableString(value.editedAt)) || (value.deletedAt !== undefined && !nullableString(value.deletedAt)))) return false;
  if (isObj(value) && value.reactions !== undefined && (!isObj(value.reactions) || Object.entries(value.reactions).some(([emoji, users]) =>
    !["👍", "❤️", "😂", "🎉", "👀", "✅"].includes(emoji) || !Array.isArray(users) || users.length > 256 || !users.every(str)))) return false;
  if (isObj(value) && value.reactionRevision !== undefined && (!Number.isInteger(value.reactionRevision) || (value.reactionRevision as number) < 0)) return false;
  if (isObj(value) && value.addressedAgentIds !== undefined && (!Array.isArray(value.addressedAgentIds) || !value.addressedAgentIds.every(str))) return false;
  if (!isObj(value) || !str(value.messageId) || !str(value.roomId) || !num(value.messageSeq) || !num(value.roomRevision)) return false;
  if (!isObj(value.author) || !authorKind(value.author.kind) || !str(value.author.id) || !str(value.author.displayName)) return false;
  return typeof value.text === "string" && Array.isArray(value.mentions) && value.mentions.every(memberRef) &&
    nullableString(value.replyToMessageId) && nullableString(value.threadRootMessageId) &&
    str(value.createdAt) && nullableString(value.runId) && (value.attachments === undefined || (
      Array.isArray(value.attachments) && value.attachments.length <= 4 && value.attachments.every((item) => {
        if (!isObj(item) || !str(item.id) || !text(item.fileName)) return false;
        const maxBytes = MESSAGE_ATTACHMENT_MAX_BYTES[item.contentType as MessageAttachment["contentType"]];
        return maxBytes !== undefined &&
          typeof item.size === "number" && Number.isInteger(item.size) && item.size > 0 && item.size <= maxBytes;
      })
    ));
}

/** Per-type validation for the exact founder-release registry. */
function payloadValid(type: LiveEventType, payload: unknown): boolean {
  if (!isObj(payload)) return false;
  switch (type) {
    case "workspace.access.removed":
      return str(payload.workspaceId) && Object.keys(payload).length === 1;
    case "workspace.sessions.changed":
      // `transcript: true` marks a streaming batch: open panes refetch, the sidebar list does not.
      // `sessionId` names the one session that changed: the list re-reads that row, not the whole list.
      return str(payload.humanUserId) && (payload.transcript === undefined || payload.transcript === true)
        && (payload.sessionId === undefined || str(payload.sessionId))
        && Object.keys(payload).every((key) => key === "humanUserId" || key === "transcript" || key === "sessionId");
    case "workspace.session.screen":
      if (payload.kind === "image") {
        return str(payload.sessionId) && str(payload.data) && num(payload.w) && num(payload.h)
          && Number.isSafeInteger(payload.seq) && (payload.seq as number) >= 0;
      }
      return str(payload.sessionId) && str(payload.generation)
        && Number.isSafeInteger(payload.epoch) && (payload.epoch as number) >= 0
        && Number.isSafeInteger(payload.seq) && (payload.seq as number) >= 0
        && Number.isInteger(payload.cols) && (payload.cols as number) >= 1 && (payload.cols as number) <= 1000
        && Number.isInteger(payload.rows) && (payload.rows as number) >= 1 && (payload.rows as number) <= 500
        && (payload.kind === "ping" ? payload.data === "" : SCREEN_KINDS.has(payload.kind as string) && text(payload.data))
        && (payload.parts === undefined || (payload.kind === "snapshot" && Number.isInteger(payload.parts) && (payload.parts as number) >= 2))
        && (payload.typist === undefined || (isObj(payload.typist) && (str(payload.typist.userId) || PERSON_COLORS.includes(payload.typist.color as never)) && str(payload.typist.name) && num(payload.typist.forMs)));
    case "workspace.session.typing":
      return str(payload.sessionId) && str(payload.userId);
    case "workspace.session.attention":
      return str(payload.sessionId) && SESSION_ATTENTION_STATES.has(payload.state as string) &&
        isObj(payload.push) && str(payload.push.title) && text(payload.push.body);
    case "room.message.updated":
      return str(payload.roomId) && str(payload.messageId) && Number.isInteger(payload.messageSeq) && (payload.messageSeq as number) > 0;
    case "room.message.created":
    case "room.message.reactions.changed":
      return canonicalMessageValid(payload);
    case "room.message.available":
      return str(payload.roomId) && str(payload.messageId) && num(payload.messageSeq) && payload.messageSeq > 0 &&
        nullableString(payload.threadRootMessageId) && typeof payload.notify === "boolean";
    case "room.typing.changed":
      return str(payload.roomId) && str(payload.userId) && text(payload.displayName) && str(payload.expiresAt);
    case "room.activity.changed":
      return str(payload.roomId) && isObj(payload.actor) && (payload.actor.kind === "human" || payload.actor.kind === "agent")
        && str(payload.actor.id) && text(payload.actor.displayName)
        && (payload.state === "typing" || payload.state === "working" || payload.state === "idle")
        && (payload.expiresAt === undefined || str(payload.expiresAt));
    case "room.run.frame":
      return str(payload.roomId) && str(payload.agentId) && str(payload.runId) &&
        (payload.threadRootMessageId === undefined || nullableString(payload.threadRootMessageId)) && chatFrameValid(payload.frame);
    case "room.browser.frame":
      return str(payload.roomId) && str(payload.agentId) && str(payload.runId) && num(payload.seq)
        && str(payload.jpegBase64) && num(payload.w) && num(payload.h)
        && (payload.tier === undefined || payload.tier === "view" || payload.tier === "thumb");
    case "room.subagent.event":
      return str(payload.roomId) && str(payload.agentId) && Array.isArray(payload.events)
        && payload.events.every((e: unknown) => isObj(e) && str(e.type) && typeof e.seq === "number" && isObj(e.payload));
    case "room.metadata.changed":
    case "room.access.removed":
      return str(payload.roomId) && num(payload.roomRevision);
    case "room.member.updated":
      return str(payload.roomId);
    case "room.subscription.rotated":
      return str(payload.roomId) && str(payload.deliveryKey) && num(payload.roomRevision);
    case "room.read.changed":
      return str(payload.roomId) && isObj(payload.reader) && str(payload.reader.id) && (
        (payload.reader.kind === "human" && Number.isInteger(payload.lastReadSeq) && (payload.lastReadSeq as number) >= 0)
        || (payload.reader.kind === "agent" && str(payload.messageId)));
    case "room.delivery.changed":
      return str(payload.roomId) && str(payload.agentId) && Number.isInteger(payload.lastDeliveredSeq) && (payload.lastDeliveredSeq as number) >= 0;
    case "room.huddle.changed":
      return str(payload.roomId) && Array.isArray(payload.userIds) && payload.userIds.every(str);
    case "runtime.operation.changed":
      return str(payload.operationId) && OPERATION_STATES.has(String(payload.state)) && str(payload.updatedAt) &&
        Object.hasOwn(payload, "result") && Object.hasOwn(payload, "error") &&
        (payload.error === null || (isObj(payload.error) && str(payload.error.code) && str(payload.error.message) && typeof payload.error.retryable === "boolean"));
    case "runtime.resource.invalidated":
      return RUNTIME_RESOURCES.has(String(payload.resource));
    case "paperclip.event":
      return PAPERCLIP_EVENT_NAMES.has(String(payload.eventName)) && Object.hasOwn(payload, "payload");
    case "digest.ready":
      return num(payload.total) && str(payload.activityUrl) && isObj(payload.countsByKind) && isObj(payload.push)
        && str(payload.push.title) && str(payload.push.body);
    case "task.changed":
      // Ids only (I1): clients refetch through the authorized API, so a status here would be ignored.
      return str(payload.taskId) && str(payload.part) && Number.isSafeInteger(payload.version);
    case "call.turn.frame":
      return str(payload.callId) && str(payload.turnId) && str(payload.agentId) && isObj(payload.frame) && str(payload.frame.type);
  }
}

/**
 * Parse one stringified AppSync event into a validated `LiveEvent`, or null.
 * Never throws and never echoes the input — a malformed frame is dropped.
 */
export function parseLiveEvent(raw: unknown): LiveEvent | null {
  let value: unknown = raw;
  if (typeof raw === "string") {
    try {
      value = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!isObj(value)) return null;
  if (value.schemaVersion !== 1) return null;
  if (!str(value.eventId) || !str(value.type) || !str(value.occurredAt)) return null;
  if (!LIVE_EVENT_TYPES.has(value.type)) return null;
  if (value.roomId !== undefined && !str(value.roomId)) return null;
  if (value.messageSeq !== undefined && !num(value.messageSeq)) return null;
  if (value.roomRevision !== undefined && !num(value.roomRevision)) return null;
  const type = value.type as LiveEventType;
  if (!payloadValid(type, value.payload)) return null;
  const payload = value.payload as Record<string, unknown>;
  // `room.member.updated` rides the user channel (not a room subscription),
  // so the backend never sets the envelope-level roomId for it -- only
  // `payload.roomId` -- and this cross-check must not demand one.
  if (type.startsWith("room.") && value.roomId !== undefined && payload.roomId !== value.roomId) return null;
  if (type === "room.message.created") {
    if (payload.messageSeq !== value.messageSeq || payload.roomRevision !== value.roomRevision) return null;
  }
  if (type === "room.message.available" && payload.messageSeq !== value.messageSeq) return null;
  const event: Record<string, unknown> = {
    schemaVersion: 1,
    eventId: value.eventId,
    type,
    occurredAt: value.occurredAt,
    payload: value.payload,
  };
  if (str(value.roomId)) event.roomId = value.roomId;
  if (num(value.messageSeq)) event.messageSeq = value.messageSeq;
  if (num(value.roomRevision)) event.roomRevision = value.roomRevision;
  return event as unknown as LiveEvent;
}
