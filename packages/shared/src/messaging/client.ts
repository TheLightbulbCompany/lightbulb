import type { ApiCallOptions, ApiMethods } from "../api/client";
import type {
  AbortRunResponse,
  CanonicalRoomMessage,
  CreateMessageRequest,
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
  SteerRunRequest,
  SteerRunResponse,
  TypingRequest,
  UpsertRoomMemberRequest,
} from "./types";

/**
 * A file for multipart upload. The DOM half is `Blob` (web `File` extends it).
 * The phone's half is a part that hands over its own bytes: Expo's `fetch`
 * builds the multipart body itself and refuses React Native's `{uri}`
 * descriptor ("Unsupported FormDataPart implementation"), which failed every
 * photo sent from the phone.
 */
export type RoomAttachmentInput =
  | Blob
  | { name: string; type: string; bytes(): Promise<Uint8Array> };

const roomPath = (roomId: string) => `/messaging/rooms/${encodeURIComponent(roomId)}`;

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
  /** `head`: only the first N bytes (≤ 16 KB, a ranged read server-side), for a preview; `signal` cancels it. */
  downloadAttachment(roomId: string, attachmentId: string, opts?: { head?: number; signal?: AbortSignal }): Promise<Blob>;
  /** A short-lived presigned URL -- how a video plays (a `<video src>` carries no bearer). */
  attachmentUrl(roomId: string, attachmentId: string): Promise<{ url: string; expiresIn: number }>;
  markRead(roomId: string, request: ReadCursorRequest): Promise<ReadCursorResponse>;
  markUnread(roomId: string, fromSeq?: number): Promise<ReadCursorResponse>;
  setMemberPrefs(roomId: string, request: MemberPrefsRequest): Promise<RoomDetail>;
  leaveRoom(roomId: string): Promise<void>;
  /** Owner-only channel delete (a tombstone; history is kept server-side). */
  deleteRoom(roomId: string): Promise<void>;
  /** What was said in the coding sessions the caller may read. */
  setPin(roomId: string, messageId: string, pinned: boolean): Promise<RoomDetail>;
  /** Forget one noted fact (the room's owner or admin). */
  forgetRoomMemory(roomId: string, messageId: string): Promise<void>;
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
    downloadAttachment: (roomId, attachmentId, opts) => api.downloadBlob(
      `${roomPath(roomId)}/attachments/${encodeURIComponent(attachmentId)}${opts?.head ? `?head=${opts.head}` : ""}`,
      opts?.signal ? { signal: opts.signal } : undefined),
    attachmentUrl: (roomId, attachmentId) => api.get(`${roomPath(roomId)}/attachments/${encodeURIComponent(attachmentId)}/url`) as Promise<{ url: string; expiresIn: number }>,
    markRead: (roomId, request) => api.put(`${roomPath(roomId)}/read`, request) as Promise<ReadCursorResponse>,
    markUnread: (roomId, fromSeq) => api.post(
      `${roomPath(roomId)}/unread`, fromSeq === undefined ? {} : { fromSeq },
    ) as Promise<ReadCursorResponse>,
    setMemberPrefs: (roomId, request) => api.patch(`${roomPath(roomId)}/me`, request) as Promise<RoomDetail>,
    async leaveRoom(roomId) {
      await api.post(`${roomPath(roomId)}/leave`, {});
    },
    async deleteRoom(roomId) {
      await api.del(roomPath(roomId));
    },
    setPin: (roomId, messageId, pinned) => (pinned
      ? api.put(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`, {})
      : api.del(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`)) as Promise<RoomDetail>,
    async forgetRoomMemory(roomId, messageId) {
      await api.del(`${roomPath(roomId)}/memory/${encodeURIComponent(messageId)}`);
    },
    async setTyping(request) {
      await api.post("/messaging/typing", request);
    },
    abortRun: (runId) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/abort`, {}) as Promise<AbortRunResponse>,
    steerRun: (runId, request) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/steer`, request) as Promise<SteerRunResponse>,
    getRunStatus: (runId) => api.get(`/messaging/runs/${encodeURIComponent(runId)}`) as Promise<RunStatusResponse>,
  };
}
