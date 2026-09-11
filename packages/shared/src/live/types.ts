/**
 * The frozen live-event contract (architecture spec §8) plus the OpenClaw
 * run/tool frame union that rides inside `room.run.frame`.
 *
 * Every public field is camelCase. `ChatIncomingMessage` is the pre-AppSync
 * gateway frame union moved here unchanged except for the two transport
 * normalizations `agent_id -> agentId` and `session_id -> sessionId`, so the
 * existing stream/tool/run reducers keep working on the same variants.
 */

import type { CanonicalRoomMessage } from "../messaging/types";

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
  | { type: "run_ended"; runId: string; outcome: "done" | "error" | "aborted"; agentId?: string; sessionId?: string }
  // Server-truth busy signal derived from OpenClaw's sessions.changed.
  | { type: "run_active"; active: boolean; agentId?: string; sessionId?: string }
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
      activity: "plan" | "patch" | "item" | "acp" | "run_status" | "compaction";
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
// Structured questions (OpenClaw `ask_user` / `secrets` tools). Mirrors
// upstream packages/gateway-protocol/src/schema/questions.ts; keep in step.
// =============================================================================

export interface QuestionOption {
  label: string;
  description?: string;
}

export interface QuestionSecretStoreBinding {
  /** Store entry name, `^[A-Z][A-Z0-9_]{0,127}$`. */
  name: string;
  kind: "secret" | "env";
  allowedHosts?: string[];
  reason?: string;
}

export interface Question {
  questionId: string;
  header: string;
  question: string;
  options: QuestionOption[];
  multiSelect?: boolean;
  isOther?: boolean;
  isSecret?: boolean;
  secretStore?: QuestionSecretStoreBinding;
  secretStoreExisting?: { updatedAtMs: number; updatedBy?: string };
}

/** Note the double nesting — `question.resolve` takes `{answers: {answers}}`. */
export interface QuestionAnswers {
  answers: Record<string, string[]>;
}

export type QuestionStatus = "pending" | "answered" | "cancelled" | "expired";

export interface QuestionRecord {
  id: string;
  questions: Question[];
  agentId?: string;
  sessionKey?: string;
  runId?: string;
  createdAtMs: number;
  expiresAtMs: number;
  status: QuestionStatus;
  answers?: QuestionAnswers;
  resolvedBy?: string;
}

export type QuestionResolvedEvent =
  | { id: string; status: "answered"; answers: QuestionAnswers }
  | { id: string; status: "cancelled" }
  | { id: string; status: "expired" };

// =============================================================================
// Live events (spec §8) — the exact founder-release registry.
// =============================================================================

export type LiveEventType =
  | "room.message.reactions.changed"
  | "room.message.created"
  | "room.typing.changed"
  | "room.run.frame"
  | "room.metadata.changed"
  | "room.subscription.rotated"
  | "room.access.removed"
  | "runtime.operation.changed"
  | "runtime.approval.requested"
  | "runtime.approval.resolved"
  | "runtime.question.requested"
  | "runtime.question.resolved"
  | "runtime.progress.changed"
  | "runtime.resource.invalidated"
  | "paperclip.event";

export const LIVE_EVENT_TYPES: ReadonlySet<string> = new Set<LiveEventType>([
  "room.message.reactions.changed",
  "room.message.created",
  "room.typing.changed",
  "room.run.frame",
  "room.metadata.changed",
  "room.subscription.rotated",
  "room.access.removed",
  "runtime.operation.changed",
  "runtime.approval.requested",
  "runtime.approval.resolved",
  "runtime.question.requested",
  "runtime.question.resolved",
  "runtime.progress.changed",
  "runtime.resource.invalidated",
  "paperclip.event",
]);

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

export type RoomTypingChanged = { roomId: string; userId: string; displayName: string; expiresAt: string };
export type RoomRunFrame = { roomId: string; agentId: string; runId: string; frame: ChatIncomingMessage };
export type RoomMetadataChanged = { roomId: string; roomRevision: number };
export type RoomSubscriptionRotated = { roomId: string; deliveryKey: string; roomRevision: number };
export type RoomAccessRemoved = { roomId: string; roomRevision: number };
export type RuntimeOperationChanged = {
  operationId: string;
  state: "queued" | "running" | "succeeded" | "failed";
  updatedAt: string;
  result: unknown | null;
  error: { code: string; message: string; retryable: boolean } | null;
};
export type RuntimeApprovalResolved = { id: string };
export type RuntimeProgressChanged = { sessionKey: string; revision: number };
export type RuntimeResource = "agents" | "config" | "models" | "skills" | "tools" | "health" | "onboarding";
export type RuntimeResourceInvalidated = { resource: RuntimeResource; resourceId?: string; revision?: number };
export type PaperclipEventName =
  | "teams.activity.logged"
  | "teams.agent.status"
  | "teams.heartbeat.run.queued"
  | "teams.heartbeat.run.status"
  | "teams.heartbeat.run.event"
  | "teams.heartbeat.run.log"
  | "teams.stream.resumed";
export type PaperclipEvent = { eventName: PaperclipEventName; payload: unknown };

export type LiveEvent =
  | LiveEventEnvelope<"room.message.created", CanonicalRoomMessage>
  | LiveEventEnvelope<"room.message.reactions.changed", CanonicalRoomMessage>
  | LiveEventEnvelope<"room.typing.changed", RoomTypingChanged>
  | LiveEventEnvelope<"room.run.frame", RoomRunFrame>
  | LiveEventEnvelope<"room.metadata.changed", RoomMetadataChanged>
  | LiveEventEnvelope<"room.subscription.rotated", RoomSubscriptionRotated>
  | LiveEventEnvelope<"room.access.removed", RoomAccessRemoved>
  | LiveEventEnvelope<"runtime.operation.changed", RuntimeOperationChanged>
  // The existing ExecApprovalRequest projection — parsed by useExecApprovals.
  | LiveEventEnvelope<"runtime.approval.requested", Record<string, unknown>>
  | LiveEventEnvelope<"runtime.approval.resolved", RuntimeApprovalResolved>
  | LiveEventEnvelope<"runtime.question.requested", QuestionRecord>
  | LiveEventEnvelope<"runtime.question.resolved", QuestionResolvedEvent>
  | LiveEventEnvelope<"runtime.progress.changed", RuntimeProgressChanged>
  | LiveEventEnvelope<"runtime.resource.invalidated", RuntimeResourceInvalidated>
  | LiveEventEnvelope<"paperclip.event", PaperclipEvent>;

export type LiveEventOf<T extends LiveEventType> = Extract<LiveEvent, { type: T }>;

const RUNTIME_RESOURCES: ReadonlySet<string> = new Set<RuntimeResource>([
  "agents", "config", "models", "skills", "tools", "health", "onboarding",
]);
const OPERATION_STATES: ReadonlySet<string> = new Set(["queued", "running", "succeeded", "failed"]);
const PAPERCLIP_EVENT_NAMES: ReadonlySet<string> = new Set<PaperclipEventName>([
  "teams.activity.logged",
  "teams.agent.status",
  "teams.heartbeat.run.queued",
  "teams.heartbeat.run.status",
  "teams.heartbeat.run.event",
  "teams.heartbeat.run.log",
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
      return ["plan", "patch", "item", "acp", "run_status", "compaction"].includes(String(value.activity)) && str(value.title) && run();
    default:
      return false;
  }
}

function canonicalMessageValid(value: unknown): value is CanonicalRoomMessage {
  if (isObj(value) && value.reactions !== undefined && (!isObj(value.reactions) || Object.entries(value.reactions).some(([emoji, users]) =>
    !["👍", "❤️", "😂", "🎉", "👀", "✅"].includes(emoji) || !Array.isArray(users) || users.length > 256 || !users.every(str)))) return false;
  if (isObj(value) && value.reactionRevision !== undefined && (!Number.isInteger(value.reactionRevision) || (value.reactionRevision as number) < 0)) return false;
  if (!isObj(value) || !str(value.messageId) || !str(value.roomId) || !num(value.messageSeq) || !num(value.roomRevision)) return false;
  if (!isObj(value.author) || !authorKind(value.author.kind) || !str(value.author.id) || !str(value.author.displayName)) return false;
  return typeof value.text === "string" && Array.isArray(value.mentions) && value.mentions.every(memberRef) &&
    nullableString(value.replyToMessageId) && nullableString(value.threadRootMessageId) &&
    str(value.createdAt) && nullableString(value.runId) && (value.attachments === undefined || (
      Array.isArray(value.attachments) && value.attachments.length <= 4 && value.attachments.every((item) => isObj(item) &&
        str(item.id) && text(item.fileName) && ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(String(item.contentType)) &&
        typeof item.size === "number" && Number.isInteger(item.size) && item.size > 0 && item.size <= 5 * 1024 * 1024)
    ));
}

/** Per-type validation for the exact founder-release registry. */
function payloadValid(type: LiveEventType, payload: unknown): boolean {
  if (!isObj(payload)) return false;
  switch (type) {
    case "room.message.created":
    case "room.message.reactions.changed":
      return canonicalMessageValid(payload);
    case "room.typing.changed":
      return str(payload.roomId) && str(payload.userId) && text(payload.displayName) && str(payload.expiresAt);
    case "room.run.frame":
      return str(payload.roomId) && str(payload.agentId) && str(payload.runId) && chatFrameValid(payload.frame);
    case "room.metadata.changed":
    case "room.access.removed":
      return str(payload.roomId) && num(payload.roomRevision);
    case "room.subscription.rotated":
      return str(payload.roomId) && str(payload.deliveryKey) && num(payload.roomRevision);
    case "runtime.operation.changed":
      return str(payload.operationId) && OPERATION_STATES.has(String(payload.state)) && str(payload.updatedAt) &&
        Object.hasOwn(payload, "result") && Object.hasOwn(payload, "error") &&
        (payload.error === null || (isObj(payload.error) && str(payload.error.code) && str(payload.error.message) && typeof payload.error.retryable === "boolean"));
    case "runtime.approval.requested":
      return str(payload.id);
    case "runtime.approval.resolved":
      return str(payload.id);
    case "runtime.question.requested":
      return str(payload.id) && Array.isArray(payload.questions) && payload.questions.length > 0;
    case "runtime.question.resolved":
      return str(payload.id) && ["answered", "cancelled", "expired"].includes(String(payload.status));
    case "runtime.progress.changed":
      return str(payload.sessionKey) && num(payload.revision);
    case "runtime.resource.invalidated":
      return RUNTIME_RESOURCES.has(String(payload.resource));
    case "paperclip.event":
      return PAPERCLIP_EVENT_NAMES.has(String(payload.eventName)) && Object.hasOwn(payload, "payload");
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
  if (type.startsWith("room.") && payload.roomId !== value.roomId) return null;
  if (type === "room.message.created") {
    if (payload.messageSeq !== value.messageSeq || payload.roomRevision !== value.roomRevision) return null;
  }
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
