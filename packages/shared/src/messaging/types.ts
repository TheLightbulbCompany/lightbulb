export type RoomKind = "channel" | "dm" | "group-dm";
export type MemberKind = "human" | "agent";
export type MemberRole = "owner" | "admin" | "member";
export type ListenMode = "active" | "mentions" | "observe";

export interface MemberRef {
  kind: MemberKind;
  id: string;
}

export type ApiErrorCode =
  | "LOCAL_SESSION_FORBIDDEN"
  | "LOCAL_SESSION_NOT_FOUND"
  | "LOCAL_SESSION_CONFLICT"
  | "LOCAL_SESSION_UNSUPPORTED"
  | "LOCAL_SESSION_OFFLINE"
  | "INVALID_REQUEST"
  | "ROOM_FORBIDDEN"
  | "ROOM_NOT_FOUND"
  | "MEMBER_NOT_FOUND"
  | "MESSAGE_NOT_FOUND"
  | "RUN_NOT_FOUND"
  | "OPERATION_NOT_FOUND"
  | "IDEMPOTENCY_CONFLICT"
  | "MESSAGE_CONFLICT"
  | "MEMBER_LIMIT"
  | "RUN_NOT_ACTIVE"
  | "RATE_LIMITED"
  | "RUNTIME_UNAVAILABLE"
  | "INTERNAL_ERROR";

export interface MessagingApiError {
  code: ApiErrorCode;
  message: string;
  retryable: boolean;
  reconcileFromSeq?: number;
  retryAfter?: number;
  details?: Record<string, unknown>;
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
}

export interface RoomDetail extends RoomSummary {
  deliveryKey: string;
  members: RoomMember[];
  lastReadSeq: number;
  createdAt: string;
}

export interface RoomListResponse {
  rooms: RoomSummary[];
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
  name: string | null;
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
  attachments?: ImageAttachment[];
}

export interface ImageAttachment {
  id: string;
  fileName: string;
  contentType: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  size: number;
}

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
  sessionId: string;
  deviceId: string;
  humanUserId: string;
  provider: "claude" | "codex";
  generation: string;
  status: "idle" | "busy" | "waiting-approval" | "offline";
  activeTurnId: string | null;
  capabilities: LocalSessionCapabilities;
  capabilitiesVerified: boolean;
}

export interface RoomResources {
  devices: LocalDevice[];
  sessions: LocalSession[];
}

export interface LocalSessionEvent {
  seq: number;
  kind: "text" | "tool" | "lifecycle";
  text: string;
  role: "user" | "assistant" | "system" | "tool";
  truncated: boolean;
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

export interface LocalSessionCommandReceipt {
  requestId: string;
  expectedGeneration: string;
  kind: "send" | "interrupt";
  text: string | null;
  expectedTurnId: string | null;
  sessionId: string;
  roomId: string;
  actorUserId: string;
  commandSeq: number;
  /** applied acknowledges admission to the harness, not successful work. */
  state: "pending" | "dispatched" | "applied" | "rejected" | "unknown";
  createdAt: number;
  /** Unix seconds; a delivery deadline, never a history TTL. */
  deliverBy: number;
}

export interface LocalSessionEnrollmentRequest {
  deviceId: string;
  cliDeviceId?: string | null;
  enabled: boolean;
}

export interface LocalSessionEnrollment extends LocalDevice {
  enabled: boolean;
}
