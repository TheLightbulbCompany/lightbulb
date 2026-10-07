export type RoomKind = "channel" | "dm" | "group-dm";
export type MemberKind = "human" | "agent";
export type MemberRole = "owner" | "admin" | "member";
export type ListenMode = "active" | "mentions" | "observe";

export interface MemberRef {
  kind: MemberKind;
  id: string;
}

export interface RoomMember extends MemberRef {
  displayName: string;
  mentionHandle: string | null;
  role: MemberRole;
  listenMode: ListenMode | null;
  joinedAt: string;
  joinedSeq: number;
}

export interface RoomSummary {
  roomId: string;
  name: string | null;
  kind: RoomKind;
  roomRevision: number;
  lastMessageSeq: number;
  lastMessageAt: string | null;
  unreadCount: number;
  updatedAt: string;
  topic: string | null;
  starred: boolean;
  muted: boolean;
  sectionId: string | null;
  /**
   * Everyone in the room. A DM has no name of its own — it IS its counterpart —
   * so without this a client cannot label its own sidebar, and both apps used to
   * follow the room list with one `getRoom` per DM just to read a name. That
   * fan-out re-ran on every `roomRevision` bump (a rename, a delivery-key
   * rotation), which is why the sidebar kept dropping back to a loading state.
   */
  members: RoomMember[];
  lastReadSeq: number;
  /**
   * The room's own AppSync workspace segment. A shared channel lives in the
   * owning workspace, so the live channel path must use this, not the caller's
   * live-config workspace. Absent on rows the backend has not stamped yet.
   */
  workspaceId?: string | null;
  /** The platform-managed channel shared with the Lightbulb team. */
  shared?: boolean;
  /**
   * The seeded starter channel. Derived by the backend from the room's
   * deterministic id, not its name — the room is renameable, so matching on
   * "general" would follow the rename. The onboarding setup band renders here
   * and nowhere else.
   */
  isDefault?: boolean;
  /** Set while the channel is archived: history stays readable, nothing new posts. */
  archivedAt?: string | null;
  archivedBy?: string | null;
}

export interface ActiveRoomRun {
  triggerMessageId?: string | null;
  runId: string;
  agentId: string;
  startedAt?: string | null;
  lastHeartbeatAt?: string | null;
}

export interface RoomDetail extends RoomSummary {
  /** The AppSync channel secret. Deliberately absent from RoomSummary — it is
   *  only ever needed for the one room actually being subscribed. */
  deliveryKey: string;
  createdAt: string;
  pinnedMessageIds: string[];
  /**
   * Runs in progress right now, from the server's run ledger. The live
   * `run_active` frame is published ONCE, at turn start, so a client that opens
   * or reloads the room mid-run would otherwise see no "Working" bubble and no
   * Stop/Steer. Optional: a response cached before this field existed has none.
   */
  activeRuns?: ActiveRoomRun[];
}

export interface RoomSection {
  id: string;
  name: string;
}

export interface RoomListResponse {
  rooms: RoomSummary[];
  sections: RoomSection[];
}

export interface MemberPrefsRequest {
  starred?: boolean;
  muted?: boolean;
  sectionId?: string | null;
}

export interface SectionsRequest {
  sections: RoomSection[];
}

export interface CreateRoomRequest {
  clientRoomId: string;
  name: string | null;
  kind: RoomKind;
  members: Array<{
    kind: MemberKind;
    id: string;
    role: "admin" | "member";
    listenMode: ListenMode | null;
  }>;
}

export interface PatchRoomRequest {
  name?: string | null;
  topic?: string | null;
  /** Owner/admin only, channels only; never #general or the shared channel. */
  archived?: boolean;
}

export interface UpsertRoomMemberRequest {
  kind: MemberKind;
  id: string;
  role: "admin" | "member";
  listenMode: ListenMode | null;
}

export interface CanonicalRoomMessage {
  messageRevision?: number;
  editedAt?: string | null;
  deletedAt?: string | null;
  reactions?: Record<string, string[]>;
  reactionRevision?: number;
  messageId: string;
  roomId: string;
  messageSeq: number;
  roomRevision: number;
  author: {
    kind: "human" | "agent" | "system";
    id: string;
    displayName: string;
  };
  text: string;
  mentions: MemberRef[];
  replyToMessageId: string | null;
  threadRootMessageId: string | null;
  createdAt: string;
  runId: string | null;
  attachments?: MessageAttachment[];
}

export interface MessageAttachment {
  id: string;
  fileName: string;
  contentType: MessageAttachmentContentType;
  size: number;
}

export const MESSAGE_ATTACHMENT_CONTENT_TYPES = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "application/pdf",
  "application/zip",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
] as const;

export type MessageAttachmentContentType = typeof MESSAGE_ATTACHMENT_CONTENT_TYPES[number];

// Some browsers/OSes report ZIPs with the legacy alias or only a generic MIME.
// The extension is included so the native picker still offers those files.
export const MESSAGE_ATTACHMENT_ACCEPT = [...MESSAGE_ATTACHMENT_CONTENT_TYPES, "application/x-zip-compressed", ".zip", ".xlsx"].join(",");
export const MESSAGE_ATTACHMENT_MAX_COUNT = 4;

/** Return the canonical persisted type for an allowed browser File.
 * Generic/empty MIME fallback requires a matching `.zip` or `.xlsx` filename;
 * contradictory declared types/extensions are rejected instead of guessed. */
export function resolveMessageAttachmentContentType(fileName: string, contentType: string): MessageAttachmentContentType | undefined {
  const type = contentType.toLowerCase().split(";", 1)[0].trim();
  const name = fileName.toLowerCase();
  const zipName = name.endsWith(".zip");
  const xlsxName = name.endsWith(".xlsx");
  const xlsxType = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  if (type === "application/zip" || type === "application/x-zip-compressed") return zipName ? "application/zip" : undefined;
  if (zipName) return !type || type === "application/octet-stream" ? "application/zip" : undefined;
  if (type === xlsxType) return xlsxName ? xlsxType : undefined;
  if (xlsxName) return !type || type === "application/octet-stream" ? xlsxType : undefined;
  return MESSAGE_ATTACHMENT_CONTENT_TYPES.find((candidate) => candidate === type);
}

/** Mirrors the backend's per-content-type cap (`ATTACHMENT_MAX_BYTES_BY_TYPE`
 *  in `core/services/messaging.py`) -- images keep the original 5 MiB limit;
 *  documents and opaque ZIP downloads get a bounded 20 MiB limit. ZIP contents
 *  are never extracted or previewed at ingress. */
export const MESSAGE_ATTACHMENT_MAX_BYTES: Record<MessageAttachmentContentType, number> = {
  "image/png": 5 * 1024 * 1024,
  "image/jpeg": 5 * 1024 * 1024,
  "image/webp": 5 * 1024 * 1024,
  "image/gif": 5 * 1024 * 1024,
  "application/pdf": 20 * 1024 * 1024,
  "application/zip": 20 * 1024 * 1024,
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": 20 * 1024 * 1024,
};

export interface CreateMessageRequest {
  clientMessageId: string;
  text: string;
  mentions: MemberRef[];
  replyToMessageId: string | null;
  threadRootMessageId: string | null;
  attachmentIds?: string[];
}

export interface UpdateMessageRequest {
  operationId: string;
  expectedRevision: number;
  /** null deletes the public message; existing agent work is unchanged. */
  text: string | null;
}

export interface MessagePage {
  messages: CanonicalRoomMessage[];
  nextAfterSeq: number | null;
  nextBeforeSeq?: number | null;
}

export interface ReadCursorRequest {
  lastReadSeq: number;
}

export interface ReadCursorResponse {
  roomId: string;
  lastReadSeq: number;
}

export interface TypingRequest {
  roomId: string;
  isTyping: boolean;
}

export interface AbortRunResponse {
  runId: string;
  state: "aborting" | "already-terminal";
}

export interface RunStatusResponse {
  runId: string;
  state: "running" | "terminal";
}

export interface SteerRunRequest {
  text: string;
}

export interface SteerRunResponse {
  runId: string;
  state: "accepted";
}

export interface LiveConfig {
  schemaVersion: 1;
  httpEndpoint: string;
  realtimeEndpoint: string;
  region: string;
  userChannelKey: string;
  workspaceId: string;
}

/** Resources belong to human members; they are never company/room members. */
export interface LocalDevice {
  deviceId: string;
  humanUserId: string;
  /** Explicitly enrolled sibling identity; absent means no association was supplied. */
  cliDeviceId: string | null;
}

export interface LocalSessionCapabilities {
  sendMode: "none" | "queue" | "immediate";
  steer: boolean;
  interrupt: boolean;
}

export interface LocalSession {
  /** Advisory bounded local evidence, not proof that a harness refreshed files. */
  workspace?: { revision: number; coverage: "bounded" | "unknown"; changed: boolean; overlap: boolean };
  sessionId: string;
  deviceId: string;
  humanUserId: string;
  provider: "claude" | "codex";
  generation: string;
  status: "idle" | "busy" | "waiting-approval" | "unknown" | "offline" | "ended";
  providerVersion: string;
  controlUnavailableReason: "runtime-unverified" | "endpoint-unavailable" | "unsupported-version" | "history-gap" | null;
  activeTurnId: string | null;
  capabilities: LocalSessionCapabilities;
  capabilitiesVerified: boolean;
  /** Canonical last ingested event; opens recent history without loading every page. */
  latestEventSeq: number;
  /** Harness-derived: Claude ai-title or listing name, Codex first prompt. */
  title?: string | null;
  /** Basename only; never a full path. */
  cwdName?: string | null;
  lastEventAt?: string | null;
  endedAt?: string | null;
  lastAssistantMessage?: string | null;
}

export interface PairedNode {
  deviceId: string;
  humanUserId: string;
  clientId: "openclaw-macos" | "node-host";
  /** Explicit validated association only; not physical co-location or readiness. */
  macDeviceId: string | null;
}

export interface RoomResources {
  devices: LocalDevice[];
  sessions: LocalSession[];
  pairedNodes: PairedNode[];
}

export interface WorkspaceSessions { sessions: LocalSession[] }

export type LocalSessionEventActor = { userId: string } | { agentId: string };
export type LocalSessionLifecycle = "turn_start" | "turn_end" | "turn_aborted" | "compaction" | "approval_pending";
export interface LocalSessionEventTool {
  /** Claude tool name, Codex item type, or "result" on a Claude tool_result row. */
  name: string;
  status: "running" | "done" | "error";
  input?: unknown;
  result?: unknown;
  /** Total output lines when `text` is a preview. */
  lines?: number;
}
export interface LocalSessionEvent {
  seq: number;
  kind: "text" | "tool" | "thinking" | "lifecycle";
  text: string;
  role: "user" | "assistant" | "system" | "tool";
  truncated: boolean;
  /** Native row timestamp, ISO-8601. */
  ts?: string;
  itemId?: string;
  parentId?: string;
  actor?: LocalSessionEventActor;
  tool?: LocalSessionEventTool;
  lifecycle?: LocalSessionLifecycle;
}

export interface LocalSessionEventPage {
  events: LocalSessionEvent[];
  nextAfterSeq: number | null;
}

export type LocalSessionCommand = {
  /** Canonical command JSON is bounded to 8 KiB UTF-8, including these fields. */
  requestId: string;
  expectedGeneration: string;
  expectedTurnId?: string | null;
} & ({ kind: "send"; text: string } | { kind: "interrupt"; text?: never });

export type LocalSessionCommandReceipt = {
  requestId: string;
  expectedGeneration: string;
  kind: "send" | "interrupt";
  text: string | null;
  expectedTurnId: string | null;
  sessionId: string;
  roomId: string;
  commandSeq: number;
  /** applied acknowledges admission to the harness, not successful work. */
  state: "pending" | "dispatched" | "applied" | "rejected" | "unknown";
  createdAt: number;
  /** Unix seconds; a delivery deadline, never a history TTL. */
  deliverBy: number;
  held: boolean;
  providerMessageId: string | null;
} & ({ actorUserId: string; actor?: never; invocationExpiresAt?: never }
  | { actorUserId?: never; actor: { kind: "agent"; id: string }; invocationExpiresAt: number });
