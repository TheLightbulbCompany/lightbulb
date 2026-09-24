import type { ApiCallOptions, ApiMethods } from "../api/client";
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
  WorkspaceSessions,
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

/**
 * Prod incident: the server stored + returned 201 within the same second, but
 * the browser's own `fetch` for the POST never settled — no error, no
 * response, forever — so `useRooms.sendMessage`'s optimistic row sat on
 * "Sending…" as a permanent duplicate of the canonical message that had
 * already arrived over the live channel.
 *
 * There's nothing else in this package timing a mutation POST to copy: the
 * one existing precedent (`useOnboardingStatus`'s `FETCH_TIMEOUT_MS`, 10s) is
 * tuned for a lightweight status GET polled every 5-15s, where a hung fetch
 * blocks a tight loop. A message send is a one-shot user action with more
 * variable latency (persistence + fanout), so reusing that value risks
 * marking a merely-slow send "uncertain". Nothing comparable exists, so this
 * uses the documented fallback.
 */
const SEND_MESSAGE_TIMEOUT_MS = 30_000;
const HISTORY_TIMEOUT_MS = 30_000;

export interface MessagingClient {
  listResources(roomId: string, options?: { includeEnded?: boolean }): Promise<RoomResources>;
  listWorkspaceSessions(): Promise<WorkspaceSessions>;
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
  listMessages(roomId: string, afterSeq: number, limit?: number, options?: ApiCallOptions & { beforeSeq?: number }): Promise<MessagePage>;
  sendMessage(roomId: string, request: CreateMessageRequest): Promise<CanonicalRoomMessage>;
  uploadAttachment(roomId: string, image: RoomAttachmentInput): Promise<MessageAttachment>;
  downloadAttachment(roomId: string, attachmentId: string): Promise<Blob>;
  markRead(roomId: string, request: ReadCursorRequest): Promise<ReadCursorResponse>;
  markUnread(roomId: string, fromSeq?: number): Promise<ReadCursorResponse>;
  setMemberPrefs(roomId: string, request: MemberPrefsRequest): Promise<RoomDetail>;
  putSections(request: SectionsRequest): Promise<RoomListResponse>;
  leaveRoom(roomId: string): Promise<void>;
  /** Owner-only channel delete (a tombstone; history is kept server-side). */
  deleteRoom(roomId: string): Promise<void>;
  setPin(roomId: string, messageId: string, pinned: boolean): Promise<RoomDetail>;
  setTyping(request: TypingRequest): Promise<void>;
  abortRun(runId: string): Promise<AbortRunResponse>;
  steerRun(runId: string, request: SteerRunRequest): Promise<SteerRunResponse>;
  getRunStatus(runId: string): Promise<RunStatusResponse>;
}

export function createMessagingClient(api: ApiMethods): MessagingClient {
  return {
    listResources: (roomId, options) =>
      api.get(`${roomPath(roomId)}/resources${options?.includeEnded ? "?includeEnded=true" : ""}`) as Promise<RoomResources>,
    listWorkspaceSessions: () => api.get("/messaging/sessions") as Promise<WorkspaceSessions>,
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
    async listMessages(roomId, afterSeq, limit = 100, options) {
      if (options?.signal?.aborted) throw new Error("History request cancelled");
      const controller = new AbortController();
      const cancel = () => controller.abort();
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        cancel();
      }, HISTORY_TIMEOUT_MS);
      const aborted = new Promise<never>((_resolve, reject) => {
        controller.signal.addEventListener("abort", () => reject(new Error(
          timedOut ? "History request timed out. Try again." : "History request cancelled",
        )), { once: true });
      });
      options?.signal?.addEventListener("abort", cancel, { once: true });
      try {
        // Race the whole API call: token acquisition can hang before fetch
        // ever observes the signal. Late responses cannot settle this read.
        return await Promise.race([
          api.get(
            `${roomPath(roomId)}/messages?afterSeq=${encodeURIComponent(String(afterSeq))}&limit=${encodeURIComponent(String(limit))}${options?.beforeSeq === undefined ? "" : `&beforeSeq=${encodeURIComponent(String(options.beforeSeq))}`}`,
            { signal: controller.signal },
          ) as Promise<MessagePage>,
          aborted,
        ]);
      } finally {
        clearTimeout(timer);
        options?.signal?.removeEventListener("abort", cancel);
      }
    },
    sendMessage: (roomId, request) => {
      // A timeout, not a retry: the caller (`useRooms.sendMessage`) already
      // treats a non-`ApiError` rejection as "uncertain" and offers
      // `reconcileSend`, which re-reads history before deciding whether to
      // resend — exactly the right response to "did this actually go
      // through?", which is what a hung fetch can never answer on its own.
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), SEND_MESSAGE_TIMEOUT_MS);
      return (api.post(`${roomPath(roomId)}/messages`, request, { signal: controller.signal }) as Promise<CanonicalRoomMessage>)
        .finally(() => clearTimeout(timer));
    },
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
    async deleteRoom(roomId) {
      await api.del(roomPath(roomId));
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
