import type { ApiMethods } from "../api/client";
import type {
  AbortRunResponse,
  CanonicalRoomMessage,
  CreateMessageRequest,
  CreateRoomRequest,
  LiveConfig,
  MemberKind,
  MessagePage,
  PatchRoomRequest,
  ReadCursorRequest,
  ReadCursorResponse,
  RoomDetail,
  RoomListResponse,
  RunStatusResponse,
  SteerRunRequest,
  SteerRunResponse,
  TypingRequest,
  UpsertRoomMemberRequest,
} from "./types";

const roomPath = (roomId: string) => `/messaging/rooms/${encodeURIComponent(roomId)}`;

export interface MessagingClient {
  getLiveConfig(): Promise<LiveConfig>;
  listRooms(): Promise<RoomListResponse>;
  createRoom(request: CreateRoomRequest): Promise<RoomDetail>;
  getRoom(roomId: string): Promise<RoomDetail>;
  updateRoom(roomId: string, request: PatchRoomRequest): Promise<RoomDetail>;
  upsertMember(roomId: string, request: UpsertRoomMemberRequest): Promise<RoomDetail>;
  removeMember(roomId: string, kind: MemberKind, memberId: string): Promise<void>;
  listMessages(roomId: string, afterSeq: number, limit?: number): Promise<MessagePage>;
  sendMessage(roomId: string, request: CreateMessageRequest): Promise<CanonicalRoomMessage>;
  markRead(roomId: string, request: ReadCursorRequest): Promise<ReadCursorResponse>;
  setTyping(request: TypingRequest): Promise<void>;
  abortRun(runId: string): Promise<AbortRunResponse>;
  steerRun(runId: string, request: SteerRunRequest): Promise<SteerRunResponse>;
  getRunStatus(runId: string): Promise<RunStatusResponse>;
}

export function createMessagingClient(api: ApiMethods): MessagingClient {
  return {
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
    markRead: (roomId, request) => api.put(`${roomPath(roomId)}/read`, request) as Promise<ReadCursorResponse>,
    async setTyping(request) {
      await api.post("/messaging/typing", request);
    },
    abortRun: (runId) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/abort`, {}) as Promise<AbortRunResponse>,
    steerRun: (runId, request) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/steer`, request) as Promise<SteerRunResponse>,
    getRunStatus: (runId) => api.get(`/messaging/runs/${encodeURIComponent(runId)}`) as Promise<RunStatusResponse>,
  };
}
