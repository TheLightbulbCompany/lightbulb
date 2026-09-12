import type { ApiMethods } from "../api/client";
import type {
  AbortRunResponse,
  CanonicalRoomMessage,
  CreateMessageRequest,
  UpdateMessageRequest,
  MessageAttachment,
  CreateRoomRequest,
  LiveConfig,
  MemberKind,
  MemberPrefsRequest,
  MessagePage,
  PatchRoomRequest,
  ReadCursorRequest,
  ReadCursorResponse,
  RoomDetail,
  RoomListResponse,
  RunStatusResponse,
  SectionsRequest,
  SteerRunRequest,
  SteerRunResponse,
  TypingRequest,
  UpsertRoomMemberRequest,
  RoomResources,
  LocalSessionEventPage,
  LocalSessionCommand,
  LocalSessionCommandReceipt,
} from "./types";

/**
 * A file for multipart upload. The DOM half is `Blob` (web `File` extends it);
 * the react-native half is the {uri,name,type} descriptor RN's fetch streams
 * from disk — RN has no File, and the cast below is the standard shim
 * (same one apps/mobile/src/lib/attachments.ts already uses).
 */
export type RoomAttachmentInput = Blob | { uri: string; name: string; type: string };

const roomPath = (roomId: string) => `/messaging/rooms/${encodeURIComponent(roomId)}`;
const localSessionPath = (roomId: string, sessionId: string) => `${roomPath(roomId)}/local-sessions/${encodeURIComponent(sessionId)}`;

export interface MessagingClient {
  listResources(roomId: string): Promise<RoomResources>;
  listLocalSessionEvents(roomId: string, sessionId: string, afterSeq: number, limit?: number): Promise<LocalSessionEventPage>;
  submitLocalSessionCommand(roomId: string, sessionId: string, request: LocalSessionCommand): Promise<LocalSessionCommandReceipt>;
  getLocalSessionCommand(roomId: string, sessionId: string, requestId: string): Promise<LocalSessionCommandReceipt>;
  updateMessage(roomId: string, messageId: string, request: UpdateMessageRequest): Promise<CanonicalRoomMessage>;
  setReaction(roomId: string, messageId: string, emoji: string, active: boolean): Promise<CanonicalRoomMessage>;
  getLiveConfig(): Promise<LiveConfig>;
  listRooms(): Promise<RoomListResponse>;
  createRoom(request: CreateRoomRequest): Promise<RoomDetail>;
  getRoom(roomId: string): Promise<RoomDetail>;
  updateRoom(roomId: string, request: PatchRoomRequest): Promise<RoomDetail>;
  upsertMember(roomId: string, request: UpsertRoomMemberRequest): Promise<RoomDetail>;
  removeMember(roomId: string, kind: MemberKind, memberId: string): Promise<void>;
  listMessages(roomId: string, afterSeq: number, limit?: number): Promise<MessagePage>;
  sendMessage(roomId: string, request: CreateMessageRequest): Promise<CanonicalRoomMessage>;
  uploadAttachment(roomId: string, image: RoomAttachmentInput): Promise<MessageAttachment>;
  downloadAttachment(roomId: string, attachmentId: string): Promise<Blob>;
  markRead(roomId: string, request: ReadCursorRequest): Promise<ReadCursorResponse>;
  markUnread(roomId: string, fromSeq?: number): Promise<ReadCursorResponse>;
  setMemberPrefs(roomId: string, request: MemberPrefsRequest): Promise<RoomDetail>;
  putSections(request: SectionsRequest): Promise<RoomListResponse>;
  leaveRoom(roomId: string): Promise<void>;
  setPin(roomId: string, messageId: string, pinned: boolean): Promise<RoomDetail>;
  setTyping(request: TypingRequest): Promise<void>;
  abortRun(runId: string): Promise<AbortRunResponse>;
  steerRun(runId: string, request: SteerRunRequest): Promise<SteerRunResponse>;
  getRunStatus(runId: string): Promise<RunStatusResponse>;
}

export function createMessagingClient(api: ApiMethods): MessagingClient {
  return {
    listResources: (roomId) => api.get(`${roomPath(roomId)}/resources`) as Promise<RoomResources>,
    listLocalSessionEvents: (roomId, sessionId, afterSeq, limit = 50) => api.get(
      `${localSessionPath(roomId, sessionId)}/events?afterSeq=${encodeURIComponent(String(afterSeq))}&limit=${encodeURIComponent(String(limit))}`,
    ) as Promise<LocalSessionEventPage>,
    submitLocalSessionCommand: (roomId, sessionId, request) => api.post(`${localSessionPath(roomId, sessionId)}/commands`, request) as Promise<LocalSessionCommandReceipt>,
    getLocalSessionCommand: (roomId, sessionId, requestId) => api.get(`${localSessionPath(roomId, sessionId)}/commands/${encodeURIComponent(requestId)}`) as Promise<LocalSessionCommandReceipt>,
    updateMessage: (roomId, messageId, request) => api.patch(`${roomPath(roomId)}/messages/${encodeURIComponent(messageId)}`, request) as Promise<CanonicalRoomMessage>,
    setReaction: (roomId, messageId, emoji, active) => api.put(`${roomPath(roomId)}/messages/${encodeURIComponent(messageId)}/reactions`, { emoji, active }) as Promise<CanonicalRoomMessage>,
    getLiveConfig: () => api.get("/messaging/live-config") as Promise<LiveConfig>,
    listRooms: () => api.get("/messaging/rooms") as Promise<RoomListResponse>,
    createRoom: (request) => api.post("/messaging/rooms", request) as Promise<RoomDetail>,
    getRoom: (roomId) => api.get(roomPath(roomId)) as Promise<RoomDetail>,
    updateRoom: (roomId, request) => api.patch(roomPath(roomId), request) as Promise<RoomDetail>,
    upsertMember: (roomId, request) => api.post(`${roomPath(roomId)}/members`, request) as Promise<RoomDetail>,
    async removeMember(roomId, kind, memberId) {
      await api.del(`${roomPath(roomId)}/members/${kind}/${encodeURIComponent(memberId)}`);
    },
    listMessages: (roomId, afterSeq, limit = 100) => api.get(
      `${roomPath(roomId)}/messages?afterSeq=${encodeURIComponent(String(afterSeq))}&limit=${encodeURIComponent(String(limit))}`,
    ) as Promise<MessagePage>,
    sendMessage: (roomId, request) => api.post(`${roomPath(roomId)}/messages`, request) as Promise<CanonicalRoomMessage>,
    uploadAttachment: (roomId, image) => {
      const body = new FormData();
      body.append("image", image as Blob);
      return api.upload(`${roomPath(roomId)}/attachments`, body) as Promise<MessageAttachment>;
    },
    downloadAttachment: (roomId, attachmentId) => api.downloadBlob(`${roomPath(roomId)}/attachments/${encodeURIComponent(attachmentId)}`),
    markRead: (roomId, request) => api.put(`${roomPath(roomId)}/read`, request) as Promise<ReadCursorResponse>,
    markUnread: (roomId, fromSeq) => api.post(
      `${roomPath(roomId)}/unread`, fromSeq === undefined ? {} : { fromSeq },
    ) as Promise<ReadCursorResponse>,
    setMemberPrefs: (roomId, request) => api.patch(`${roomPath(roomId)}/me`, request) as Promise<RoomDetail>,
    putSections: (request) => api.put("/messaging/me/sections", request) as Promise<RoomListResponse>,
    async leaveRoom(roomId) {
      await api.post(`${roomPath(roomId)}/leave`, {});
    },
    setPin: (roomId, messageId, pinned) => (pinned
      ? api.put(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`, {})
      : api.del(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`)) as Promise<RoomDetail>,
    async setTyping(request) {
      await api.post("/messaging/typing", request);
    },
    abortRun: (runId) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/abort`, {}) as Promise<AbortRunResponse>,
    steerRun: (runId, request) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/steer`, request) as Promise<SteerRunResponse>,
    getRunStatus: (runId) => api.get(`/messaging/runs/${encodeURIComponent(runId)}`) as Promise<RunStatusResponse>,
  };
}
