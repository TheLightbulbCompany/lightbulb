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
  /** The viewer is from another workspace than the room's owner (a shared-channel guest). */
  guest?: boolean;
  /**
   * The seeded starter channel. Derived by the backend from the room's
   * deterministic id, not its name — the room is renameable, so matching on
   * "general" would follow the rename. The onboarding setup band renders here
   * and nowhere else.
   */
  isDefault?: boolean;
  /** Super-agent: the owner's Lightbulb DM holding the first-run welcome (seq 1, kind "first_run"); a fresh workspace lands here. */
  firstRun?: boolean;
  /** Set while the channel is archived: history stays readable, nothing new posts. */
  archivedAt?: string | null;
  archivedBy?: string | null;
  /** A project's tile (UX slice 6); absent = the neutral tile with the name's first letter. */
  icon?: ProjectIcon | null;
  color?: ProjectColor | null;
  /** Set while the project is paused: Lightbulb takes no new message there and its schedules skip. */
  pausedAt?: string | null;
  pausedBy?: string | null;
  /** Set once the channel's owner deleted it. The backend hides a deleted room, so clients rarely see it. */
  deletedAt?: string | null;
  /** A session's chat (hidden from the room list): the session it talks beside. Absent on every other room. */
  session?: { sessionId: string } | null;
  /** The workspace feed (Activity): listed by the server, hidden from every conversation list and unread count. */
  feed?: boolean;
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

/** The 12 lucide glyphs a project tile may use (kebab-case lucide names). */
export const PROJECT_ICONS = [
  "sparkles", "rocket", "shopping-bag", "megaphone", "code", "pen-line",
  "chart-line", "users", "calendar", "package", "leaf", "briefcase",
] as const;
export type ProjectIcon = (typeof PROJECT_ICONS)[number];
/** Colour tokens `--project-1..6`. */
export const PROJECT_COLORS = ["project-1", "project-2", "project-3", "project-4", "project-5", "project-6"] as const;
export type ProjectColor = (typeof PROJECT_COLORS)[number];
/** A project's Instructions (its room topic). */
export const ROOM_TOPIC_MAX_CHARS = 2000;

export interface CreateRoomRequest {
  clientRoomId: string;
  name: string | null;
  kind: RoomKind;
  /** Channels only (ignored on a DM). */
  topic?: string | null;
  icon?: ProjectIcon | null;
  color?: ProjectColor | null;
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
  /** Channels only; null clears. */
  icon?: ProjectIcon | null;
  color?: ProjectColor | null;
  /** Projects only. Room owner/admin or a workspace admin; audited. */
  paused?: boolean;
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
  /** A person's message a tool posted for them: the hosted MCP, Let's do it in Ideas, the front-desk
   *  build prompt, or a member's text to the workspace line. */
  via?: "lightbulb_mcp" | "ideas" | "front_desk" | "text";
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
  /** A feed post's "Why I created this post" (plain text). Absent on every other message. */
  postReason?: string | null;
  /** Super-agent attrs (contracts-v2 §6), only on messages Lightbulb posts for tasks; absent otherwise. */
  taskRefs?: { taskId: string; role: "started"; qid?: string }[] | null;
  /** Specialist handle of a result line ("finance"); the byline reads "Finance". */
  specialist?: string | null;
  /** On a person's message: where Lightbulb sent it. */
  routedTo?: { taskIds: string[]; started: number; passThrough: boolean } | null;
  /** On a task-room line: the card reply it was forwarded from. */
  forwardedFrom?: { roomId: string; messageId: string; roomName?: string } | null;
  /** "result" on a task's result line; "task_line" on the Router's one-line result in the main chat (its task is a chip);
   *  "marker" on a task room's lifecycle line ("Started", "Closed by Sam"); "automation_confirm" on the card that turns an automation on. */
  kind?: string | null;
  /** The automation an `automation_confirm` card is about (lane A's propose route). */
  automationId?: string | null;
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
  // HTML -- served as a download, rendered only in a sandboxed frame
  "text/html",
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
  "text/html": 5 * 1024 * 1024,
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
  /** Clients do not read it. The backend sends `{ superAgent: true }` for every workspace once the switch-retirement backend PR ships; kept on the wire for old app builds. */
  features?: { superAgent: boolean } | null;
}
