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

/** `META.lastMessage`: the newest live top-level message as a <=140-char plain preview. */
export interface RoomLastMessage {
  messageId: string;
  seq: number;
  authorId: string;
  authorKind: "human" | "agent" | "system";
  authorName: string;
  textPreview: string;
  hasAttachments: boolean;
  createdAt: string;
  threadRootMessageId: string | null;
}

export interface RoomSummary {
  roomId: string;
  name: string | null;
  kind: RoomKind;
  roomRevision: number;
  lastMessageSeq: number;
  /** The latest live top-level message's time, else the room's createdAt; the list is sorted on it. */
  lastMessageAt: string | null;
  lastMessage?: RoomLastMessage | null;
  /** Top-level messages above my read cursor, never my own, capped at 99. */
  unreadCount: number;
  /** Messages @mentioning me above my read cursor, thread replies included, capped at 99. */
  mentionCount?: number;
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
  /** Set once the channel's owner deleted it. The backend hides a deleted room, so clients rarely see it. */
  deletedAt?: string | null;
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
  // Office (OOXML) documents -- zip containers under the hood, same as
  // application/zip above; validated the same way (outer zip signature only,
  // no internal structure check).
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", // .xlsx
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", // .docx
  "application/vnd.openxmlformats-officedocument.presentationml.presentation", // .pptx
  // Plain text -- no fixed byte signature; validated as UTF-8 instead.
  "text/csv",
  "text/plain",
  "text/markdown",
] as const;

export type MessageAttachmentContentType = typeof MESSAGE_ATTACHMENT_CONTENT_TYPES[number];

// Some browsers/OSes report ZIPs with the legacy alias or only a generic MIME.
// The extension is included so the native picker still offers those files.
export const MESSAGE_ATTACHMENT_ACCEPT = [...MESSAGE_ATTACHMENT_CONTENT_TYPES, "application/x-zip-compressed", ".zip"].join(",");
export const MESSAGE_ATTACHMENT_MAX_COUNT = 4;

/** Return the canonical persisted type for an allowed browser File.
 * Generic/empty MIME fallback is ZIP-only and requires a `.zip` filename;
 * contradictory declared types/extensions are rejected instead of guessed. */
export function resolveMessageAttachmentContentType(fileName: string, contentType: string): MessageAttachmentContentType | undefined {
  const type = contentType.toLowerCase().split(";", 1)[0].trim();
  const zipName = fileName.toLowerCase().endsWith(".zip");
  if (type === "application/zip" || type === "application/x-zip-compressed") return zipName ? "application/zip" : undefined;
  if (zipName) return !type || type === "application/octet-stream" ? "application/zip" : undefined;
  return MESSAGE_ATTACHMENT_CONTENT_TYPES.find((candidate) => candidate === type);
}

/** Mirrors the backend's per-content-type cap (`ATTACHMENT_MAX_BYTES_BY_TYPE`
 *  in `core/services/messaging.py`) -- images keep the original 5 MiB limit;
 *  PDFs, ZIPs and Office docs (all zip containers) get a bounded 20 MiB
 *  limit; plain text reuses the image tier at 5 MiB (already generous for a
 *  spreadsheet export or a long log). ZIP/Office contents are never
 *  extracted or previewed. */
export const MESSAGE_ATTACHMENT_MAX_BYTES: Record<MessageAttachmentContentType, number> = {
  "image/png": 5 * 1024 * 1024,
  "image/jpeg": 5 * 1024 * 1024,
  "image/webp": 5 * 1024 * 1024,
  "image/gif": 5 * 1024 * 1024,
  "application/pdf": 20 * 1024 * 1024,
  "application/zip": 20 * 1024 * 1024,
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": 20 * 1024 * 1024,
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 20 * 1024 * 1024,
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": 20 * 1024 * 1024,
  "text/csv": 5 * 1024 * 1024,
  "text/plain": 5 * 1024 * 1024,
  "text/markdown": 5 * 1024 * 1024,
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
  /** False while its owner has paused sharing it; only they can see it then. Absent means shared. */
  shared?: boolean;
  /** The owner's "Let teammates control this session"; off by default, off again at session end. */
  teamControl?: boolean;
  /** A permission prompt the owner's Mac holds open for a remote answer. */
  pendingApproval?: LocalSessionPendingApproval;
  /** As the harness wrote it. Read-only: neither CLI takes a change from outside its terminal. */
  model?: string;
  permissionMode?: string;
}

export type LocalSessionApprovalMode = "acceptEdits" | "plan" | "default";
export interface LocalSessionPendingApproval {
  approvalId: string;
  tool: string;
  summary: string;
  /** SHA-256 of the exact tool input; an answer binds to it. */
  inputDigest: string;
  /** False when what is shown is not all of it (a cut or redacted command): deny-only here. */
  exact: boolean;
  /** Session-scoped mode switches the tool itself offered with this prompt. */
  modes: LocalSessionApprovalMode[];
}

export interface LocalSessionAuditEntry {
  at: string;
  action: "send" | "interrupt" | "approve" | "deny" | "team_control_on" | "team_control_off" | "team_control_ended" | "share_link_created" | "share_link_revoked";
  actorUserId?: string;
  actorAgentId?: string;
  requestId?: string;
  detail?: string;
  state?: LocalSessionCommandReceipt["state"];
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
} & ({ kind: "send"; text: string } | { kind: "interrupt"; text?: never }
  | { kind: "approve"; approvalId: string; decision: "allow" | "deny"; inputDigest: string; mode?: LocalSessionApprovalMode; text?: string });

export type LocalSessionCommandReceipt = {
  requestId: string;
  expectedGeneration: string;
  kind: "send" | "interrupt" | "approve";
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

// ---------------------------------------------------------------------------
// Activity feed (GET /messaging/activity). Everything a client shows is
// server-rendered: render `summary` and `actions` generically, never compose
// text per kind. New kinds (reaction, agent_run) arrive without a shape change.
// ---------------------------------------------------------------------------

export type ActivityKind = "mention" | "dm" | "thread_reply" | "reaction" | "agent_run" | "paperclip" | "session";
/** `interaction`: an agent paused on a task, waiting for a human to confirm or answer. */
/** `waiting` / `finished`: one of your shared sessions paused for your input, or ended a long turn. */
export type ActivitySubtype = "approval" | "assigned_issue" | "failed_run" | "interaction" | "waiting" | "finished";
export type ActivityLinkKind = "issue" | "room" | "thread" | "run" | "approval" | "session";

export interface ActivityActor {
  kind: "human" | "agent" | "system";
  /**
   * `agent`: the agent id (same as its room member id). `human`: the Clerk user id —
   * except a Paperclip teammate we cannot map to a workspace member, who keeps
   * their Paperclip user id (a different id space; do not look it up as a Clerk id).
   */
  id: string;
  name: string;
}

export interface ActivitySummary {
  /** One human sentence, <=120 chars. */
  title: string;
  requester: ActivityActor;
  reason: string | null;
  preview: {
    /** Plain text, <=600 chars. */
    text: string | null;
    destination: string | null;
    /** <=8 redacted key/values. */
    fields: { label: string; value: string }[];
  } | null;
  links: { kind: ActivityLinkKind; id: string; label: string }[];
}

export interface ActivityActionInput {
  kind: "text" | "select" | "person";
  label: string;
  required: boolean;
  /** `select` only. A `person` input lists the workspace roster client-side; the server validates the id. */
  options: { value: string; label: string }[] | null;
}

export interface ActivityAction {
  id: string;
  label: string;
  style: "primary" | "destructive" | null;
  /** Ask before running. */
  confirm: string | null;
  input: ActivityActionInput | null;
  /** Client-side only: never POSTed. */
  navigate: { kind: ActivityLinkKind; id: string; roomId: string | null } | null;
}

export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  subtype: ActivitySubtype | null;
  roomId: string | null;
  messageId: string | null;
  threadRootMessageId: string | null;
  actor: ActivityActor;
  createdAt: string;
  state: "open" | "done" | "resolved";
  resolvedBy: ActivityActor | null;
  resolvedAt: string | null;
  read: boolean;
  summary: ActivitySummary;
  /** Computed for the viewer; empty once resolved. */
  actions: ActivityAction[];
}

export interface ActivityPage {
  items: ActivityItem[];
  /** Opaque; pass back as `cursor`. Null on the last page. */
  next: string | null;
  /** First page only (null after it): the Activity badge, capped at 99. */
  unseenCount: number | null;
  /**
   * First page only: the rail/tab badge, stock Paperclip's inbox count -- every
   * "needs you" item (open approval, failed run) seen or not, plus the unseen rest.
   * Capped at 99. Absent from an older server.
   */
  attentionCount?: number | null;
  sources: { paperclip: "ok" | "unavailable" };
}

export interface ActivitySeenResponse {
  seenUpTo: string | null;
}

export interface ActivityActionRequest {
  /** Approve/reject/request-changes note. */
  note?: string;
  /** Reply text, a `select` value, or a `person` id. */
  value?: string;
  /**
   * Mint a fresh UUID per tap (the PlatformAdapter's uuid) and reuse it on a retry:
   * a retried reply posts once, a second deliberate identical reply posts again.
   */
  idempotencyKey?: string;
}
