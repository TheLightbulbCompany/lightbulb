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
  /** The viewer is from another workspace than the room's owner (a shared-channel guest). */
  guest?: boolean;
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
  /** A session's chat (hidden from the room list): the session it talks beside. Absent on every other room. */
  session?: { sessionId: string } | null;
}

export interface ActiveRoomRun {
  triggerMessageId?: string | null;
  /** The thread the run answers in; `null` is the main conversation. Absent from older backends. */
  threadRootMessageId?: string | null;
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
  /** Every CURRENT human member's read cursor (the viewer's included). Kept live by
   *  `room.read.changed`. Optional: a response cached before this field existed has none. */
  readCursors?: { userId: string; lastReadSeq: number }[];
  /** Every CURRENT agent member's delivery cursor. Kept live by `room.delivery.changed`.
   *  Optional: a response cached before this field existed has none. */
  agentDeliveries?: { agentId: string; lastDeliveredSeq: number }[];
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
  via?: "lightbulb_mcp";
  /** A mid-run progress line, not the run's reply: the run card stays. Absent otherwise. */
  progress?: boolean;
  /** The agents this message addresses -- an @mention, a name-addressed open
   *  ("Hey Friday, ..."), or a DM. Excludes an `active` listener Jev was merely
   *  eligible to route: Jev picks at most one of them (or none), so listing
   *  every candidate would call a plain message "Delivered" to an agent nobody
   *  chose to run. Absent on a backend/cache that predates this field; present
   *  (possibly []) on every message a current backend serves. See
   *  readState.ts's `addressed`. */
  addressedAgentIds?: string[];
  attachments?: MessageAttachment[];
  /** Session chips: references, like document links. They never message the session. */
  sessionMentions?: string[];
  /** "<Provider> · <title> · <owner>" per mentioned session, captured when the message was sent. */
  sessionTargetLabels?: Record<string, string>;
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
  // Video -- agent-sent only (the runtime uploads straight to S3); clients play
  // it from `attachmentUrl`, never through the byte route.
  "video/mp4",
  "video/webm",
  "video/quicktime",
] as const;

export type MessageAttachmentContentType = typeof MESSAGE_ATTACHMENT_CONTENT_TYPES[number];

export const isVideoAttachmentType = (contentType: string): boolean => contentType.startsWith("video/");

/** What a human may pick in the composer: everything but video, whose 100 MiB
 *  cap cannot ride the upload route (API Gateway caps a body at 10 MB). */
export const MESSAGE_ATTACHMENT_UPLOAD_CONTENT_TYPES = MESSAGE_ATTACHMENT_CONTENT_TYPES.filter((type) => !isVideoAttachmentType(type));

// Some browsers/OSes report ZIPs with the legacy alias or only a generic MIME.
// The extension is included so the native picker still offers those files.
export const MESSAGE_ATTACHMENT_ACCEPT = [...MESSAGE_ATTACHMENT_UPLOAD_CONTENT_TYPES, "application/x-zip-compressed", ".zip"].join(",");
export const MESSAGE_ATTACHMENT_MAX_COUNT = 4;

/** Return the canonical persisted type for an allowed browser File.
 * Generic/empty MIME fallback is ZIP-only and requires a `.zip` filename;
 * contradictory declared types/extensions are rejected instead of guessed. */
export function resolveMessageAttachmentContentType(fileName: string, contentType: string): MessageAttachmentContentType | undefined {
  const type = contentType.toLowerCase().split(";", 1)[0].trim();
  const zipName = fileName.toLowerCase().endsWith(".zip");
  if (type === "application/zip" || type === "application/x-zip-compressed") return zipName ? "application/zip" : undefined;
  if (zipName) return !type || type === "application/octet-stream" ? "application/zip" : undefined;
  return MESSAGE_ATTACHMENT_UPLOAD_CONTENT_TYPES.find((candidate) => candidate === type);
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
  "video/mp4": 100 * 1024 * 1024,
  "video/webm": 100 * 1024 * 1024,
  "video/quicktime": 100 * 1024 * 1024,
};

export interface CreateMessageRequest {
  clientMessageId: string;
  text: string;
  mentions: MemberRef[];
  sessionMentions?: string[];
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

export interface MessageSearchHit {
  messageId: string;
  roomId: string;
  messageSeq: number;
  threadRootMessageId: string | null;
  authorName: string;
  authorKind: string;
  createdAt: string;
  /** Matches are wrapped in \u0001...\u0002. */
  snippet: string;
}

export interface MessageSearchResponse {
  hits: MessageSearchHit[];
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
  /** The owner's exact remote launch operation, present only for that launch generation. */
  startId?: string;
  controlUnavailableReason: "runtime-unverified" | "endpoint-unavailable" | "unsupported-version" | "history-gap" | null;
  activeTurnId: string | null;
  capabilities: LocalSessionCapabilities;
  capabilitiesVerified: boolean;
  /** Canonical last ingested event; opens recent history without loading every page. */
  latestEventSeq: number;
  kind?: "interactive" | "background";
  /** Which live terminal a running session has: "held" (Lightbulb runs it) or "attach" (a Claude
   *  background session). Absent: its transcript is all there is. */
  terminal?: "held" | "attach";
  /** Why a running session has no live terminal, as its Mac reports it (spec 2026-10-07 §5.1). */
  terminalReason?: "plain" | "no-daemon" | "app" | "sdk" | "unknown" | "started-before-folder-approved";
  /** An ended held session whose final screen its owner's Mac still shows; `terminal` stays set while it is. */
  screenAvailable?: boolean;
  /** An ended session that was held: its kept screen may still be on its way from the Mac. */
  wasHeld?: boolean;
  /** The session this one continues: a Claude session moved to the background (←) forks a new one. */
  forkedFrom?: string;
  /** Harness-derived: Claude ai-title or listing name, Codex first prompt. */
  title?: string | null;
  /** Owner-chosen name shared by Lightbulb surfaces. Null uses the harness title. */
  displayTitle?: string | null;
  /** Basename only; never a full path. Shown bare: "~/" before it would claim a place it does not have. */
  cwdName?: string | null;
  lastEventAt?: string | null;
  endedAt?: string | null;
  lastAssistantMessage?: string | null;
  /** False while its owner has paused sharing it; only they can see it then. Absent means shared. */
  shared?: boolean;
  /** The owner's "Let teammates control this session"; off by default, off again at session end. */
  teamControl?: boolean;
  /** People the owner let in after they asked to join: each may send and answer prompts. Cleared at session end. */
  allowedSenders?: string[];
  /** The READER's own ask to join: pending, or declined by the owner. Absent otherwise. */
  joinRequest?: "pending" | "declined" | null;
  /** The reader's own request's id, so a reply can be told apart from a newer answer. */
  joinRequestId?: string | null;
  /** The owner's own view only: teammates waiting for an answer to "Ask to join". */
  pendingJoins?: string[];
  /** A request is pending and the owner's older Mac (no bandOwnerOnly) has paused remote typing until it is answered. A newer Mac never pauses. */
  inputPaused?: boolean;
  /** The viewer's seat in this session's chat, once they are in it. */
  chatRoomId?: string;
  /** Top-level chat messages above the viewer's read point, capped at 99. */
  chatUnread?: number;
  /** The owner's own view only: each waiting person's request id, sent back with the answer. */
  pendingJoinIds?: Record<string, string>;
  /** The READER's own ask to pick this session up: pending, or declined by the owner. */
  pickupRequest?: "pending" | "declined" | null;
  /** The owner's own view only: teammates waiting for an answer to "Ask to pick up", and their request ids. */
  pendingPickups?: string[];
  pendingPickupIds?: Record<string, string>;
  /** A permission prompt the owner's Mac holds open for a remote answer. */
  pendingApproval?: LocalSessionPendingApproval;
  /** As the harness wrote it. Read-only: neither CLI takes a change from outside its terminal. */
  model?: string;
  permissionMode?: string;
  /** Tokens as the harness reports them; present for the owner, and for teammates once shared. */
  usage?: LocalSessionUsage;
  shareUsage?: boolean;
  /** The owner's "Let teammates pick up this session" and the snapshot it produced; absent until allowed. */
  pickup?: LocalSessionPickup;
  /** Set on a session that continues a teammate's: whose, and which. */
  pickedUpFrom?: { sessionId: string; humanUserId: string };
  /** Ended sessions only: who besides the owner may resume it (the people who could send when it ended). */
  resumeAllowed?: string[];
  /** A resume waiting for the owner's Mac, or the Mac's refusal of one. */
  resume?: LocalSessionResume;
  /** Ended, not background, and the workspace may start sessions on its Macs (server-computed). */
  resumable?: boolean;
}

/** A resume someone asked for on an ended session: waiting for the owner's Mac, or refused by it. */
export interface LocalSessionResume {
  state: "pending" | "failed";
  byUserId: string;
  requestedAt: string;
  /** Why the Mac refused: ALREADY_RUNNING, FOLDER_MISSING, FOLDER_NOT_APPROVED, TOOL_UNAVAILABLE, SHARING_OFF, LAUNCH_FAILED. */
  reason?: string;
}

/**
 * Pick-up: a teammate continues this conversation on their own Mac, with the files it changed.
 * Nothing is uploaded until the owner allows it; the snapshot is the full transcript and the
 * working tree, unredacted, kept until 7 days after the session ends.
 */
export interface LocalSessionPickup {
  allowed: boolean;
  /** preparing: allowed, nothing uploaded yet. unavailable: the owner's Mac could not snapshot it. Absent while not allowed. */
  state?: "preparing" | "ready" | "unavailable";
  snapshotAt?: string | null;
  bytes?: number;
  /** False for a conversation with no repository to take files from. */
  hasCode?: boolean;
  /** "github.com/org/repo". */
  repo?: string | null;
  branch?: string | null;
  expiresAt?: string | null;
  reason?: string | null;
  /** The repository is private: its folder name is hidden from outsiders. */
  privateRepo?: boolean;
}

export interface LocalSessionPickupPart { url: string; bytes: number; sha256: string }

/** What the receiving Mac needs to continue a session: short-lived links and what to check them against. */
export interface LocalSessionPickupTicket {
  pickupId: string;
  session: { sessionId: string; humanUserId: string; title?: string | null; provider: "claude" | "codex" };
  manifest: Record<string, unknown>;
  downloads: {
    transcript: (LocalSessionPickupPart & { index: number })[];
    code: LocalSessionPickupPart | null;
    sidecars: LocalSessionPickupPart | null;
  };
  expiresAt: string;
}

export interface LocalSessionPickupOutcome {
  ok: boolean;
  code?: string;
  branch?: string;
  deviceId?: string;
  profileFingerprint?: string;
  conversationId?: string;
}

export interface LocalSessionUsage { inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheWriteTokens: number }

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

export interface PrivateSendApproval {
  approvalId: string;
  inputDigest: string;
  destination: string;
  text: string;
  requestId: string;
}

export interface LocalSessionAuditEntry {
  at: string;
  action: "native_send_requested" | "native_send_approved" | "native_send_denied" | "native_send_cancelled" | "send" | "interrupt" | "end" | "approve" | "deny" | "cancel" | "team_control_on" | "team_control_off" | "team_control_ended" | "share_link_created" | "share_link_revoked"
    | "share_link_audience" | "session_copied"
    | "pickup_on" | "pickup_off" | "picked_up" | "pickup_expired"
    | "pickup_requested" | "pickup_request_declined" | "join_requested" | "join_allowed" | "join_declined" | "sender_removed"
    | "resume_requested" | "resume_revoked";
  actorUserId?: string;
  /** Whom an owner's join decision or removal was about. */
  subjectUserId?: string;
  actorAgentId?: string;
  requestId?: string;
  detail?: string;
  state?: LocalSessionCommandReceipt["state"];
}

export type SessionLinkAudience = "workspace" | "anyone";
/** The session's one link. `url` is null only when it can no longer be shown (turn it off and on). */
export interface SessionLink { url: string | null; audience: SessionLinkAudience }
export interface SessionLinkCode { visibility: "public" | "private" | "none" | "workspace"; remote?: string; branch?: string; commit?: string }
export interface SessionLinkPage {
  title: string | null; ownerFirstName: string; provider: "claude" | "codex"; state: "live" | "ended" | "offline";
  code: SessionLinkCode; continuable: boolean; me: { signedIn: boolean; member: boolean }; sessionId?: string;
}
export interface SessionCopyCode { code: string }

export interface WorkspaceSessions { sessions: LocalSession[] }

export type LocalSessionEventActor = { userId: string } | { agentId: string };
export type LocalSessionLifecycle = "turn_start" | "turn_end" | "turn_aborted" | "compaction" | "approval_pending" | "subagent_start" | "subagent_end";
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
  /** Exact Lightbulb request that supplied this native Codex user prompt, when present. */
  originRequestId?: string;
  parentId?: string;
  actor?: LocalSessionEventActor;
  tool?: LocalSessionEventTool;
  lifecycle?: LocalSessionLifecycle;
  /** The subagent this event belongs to (a Claude agent id, a Codex child thread); absent for the session itself. */
  subagent?: string;
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
} & ({ kind: "send"; text: string } | { kind: "interrupt"; text?: never } | { kind: "end"; text?: never; expectedTurnId?: never }
  | { kind: "approve"; approvalId: string; decision: "allow" | "deny"; inputDigest: string; mode?: LocalSessionApprovalMode; text?: string });

export type LocalSessionCommandReceipt = {
  requestId: string;
  expectedGeneration: string;
  kind: "send" | "interrupt" | "approve" | "end";
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
  cancelledAt?: number;
  providerMessageId: string | null;
  nativeHop?: 1 | 2;
} & ({ actorUserId: string; actor?: never; invocationExpiresAt?: never }
  | { actorUserId?: never; actor: { kind: "agent" | "session"; id: string }; invocationExpiresAt: number });

export interface LocalSessionOfflineOutbox {
  commands: LocalSessionCommandReceipt[];
}

// ---------------------------------------------------------------------------
// Activity feed (GET /messaging/activity). Everything a client shows is
// server-rendered: render `summary` and `actions` generically, never compose
// text per kind. New kinds (reaction, agent_run) arrive without a shape change.
// ---------------------------------------------------------------------------

export type ActivityKind = "mention" | "dm" | "thread_reply" | "reaction" | "agent_run" | "paperclip" | "session" | "login" | "extension";
/** `interaction`: an agent paused on a task, waiting for a human to confirm or answer. */
/** `waiting` / `finished`: one of your shared sessions paused for your input, or ended a long turn. */
/** `login`: an agent needs a website login. (A sign-in code is typed into the shared browser, never asked for here.) */
export type ActivitySubtype = "approval" | "assigned_issue" | "failed_run" | "interaction" | "waiting" | "finished" | "join_request" | "login" | "extension";
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
  /** `login` (username + password) is a secret form: the answer is posted as `value` only. */
  kind: "text" | "select" | "person" | "login";
  label: string;
  required: boolean;
  /** `select` only. A `person` input lists the workspace roster client-side; the server validates the id. */
  options: { value: string; label: string }[] | null;
  /** `login` only: a line shown above the fields, e.g. why a password is still needed. */
  hint?: string | null;
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
