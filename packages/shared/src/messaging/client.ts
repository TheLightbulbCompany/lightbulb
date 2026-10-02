import type { ApiCallOptions, ApiMethods } from "../api/client";
import type {
  AbortRunResponse,
  ActivityActionRequest,
  ActivityItem,
  ActivityPage,
  ActivitySeenResponse,
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
  WorkspaceSessions,
  LocalSession,
  PrivateSendApproval,
  NativeSendDecision,
  NativeSendReceipt,
  LocalSessionEventPage,
  LocalSessionCommand,
  LocalSessionCommandReceipt,
  LocalSessionOfflineOutbox,
  LocalSessionAuditEntry,
  LocalSessionShareLink,
  LocalSessionPickupOutcome,
  LocalSessionPickupTicket,
  SharedLocalSession,
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
const sessionPath = (sessionId: string) => `/messaging/sessions/${encodeURIComponent(sessionId)}`;

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
  /** `includeEnded` appends sessions that ended in the last 7 days (the sidebar's "Recent"). */
  listWorkspaceSessions(options?: { includeEnded?: boolean }): Promise<WorkspaceSessions>;
  /** One session, ended included -- the list keeps only live ones. 404s LOCAL_SESSION_NOT_FOUND otherwise. */
  getLocalSession(sessionId: string): Promise<LocalSession>;
  /** Owner-only display name; null restores the provider title. */
  setLocalSessionDisplayTitle(sessionId: string, displayTitle: string | null): Promise<LocalSession>;
  getPrivateSendApproval(sessionId: string): Promise<PrivateSendApproval>;
  answerNativeSendApproval(sessionId: string, decision: NativeSendDecision): Promise<NativeSendReceipt>;
  getNativeSendReceipt(sessionId: string, requestId: string): Promise<NativeSendReceipt>;
  listLocalSessionEvents(sessionId: string, afterSeq: number, limit?: number): Promise<LocalSessionEventPage>;
  submitLocalSessionCommand(sessionId: string, request: LocalSessionCommand, replacesRequestId?: string): Promise<LocalSessionCommandReceipt>;
  getLocalSessionCommand(sessionId: string, requestId: string): Promise<LocalSessionCommandReceipt>;
  cancelLocalSessionCommand(sessionId: string, requestId: string): Promise<LocalSessionCommandReceipt>;
  /** Explicit recovery of one failed room mention; requestId is new, expectedRequestId fences the visible send. */
  retrySessionSend(roomId: string, messageId: string, sessionId: string, request: { requestId: string; expectedRequestId: string | null }): Promise<CanonicalRoomMessage>;
  cancelSessionSend(roomId: string, messageId: string, sessionId: string, request: { expectedRequestId: string | null }): Promise<CanonicalRoomMessage>;
  listLocalSessionOutbox(sessionId: string): Promise<LocalSessionOfflineOutbox>;
  /** Owner only: pause or resume sharing one session. */
  setLocalSessionSharing(sessionId: string, shared: boolean): Promise<LocalSession>;
  /** Owner-only: let workspace members use this session's controls. */
  setLocalSessionTeamControl(sessionId: string, enabled: boolean): Promise<LocalSession>;
  /** A teammate asks the session's owner to let them send. Repeating while pending changes nothing. */
  requestLocalSessionJoin(sessionId: string): Promise<LocalSession>;
  /** The owner allows or declines one person's ask. Allow lets just that person send. */
  decideLocalSessionJoin(sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string): Promise<LocalSession>;
  /** The owner takes one allowed person's access back. */
  removeLocalSessionSender(sessionId: string, userId: string): Promise<LocalSession>;
  listLocalSessionAudit(sessionId: string): Promise<{ entries: LocalSessionAuditEntry[] }>;
  /** Owner-only snapshot links, opened only by signed-in members of the owner's workspace. */
  createLocalSessionShareLink(sessionId: string, expiresInDays: number): Promise<LocalSessionShareLink>;
  listLocalSessionShareLinks(sessionId: string): Promise<{ links: LocalSessionShareLink[] }>;
  revokeLocalSessionShareLink(sessionId: string, linkId: string): Promise<void>;
  getSharedLocalSession(token: string): Promise<SharedLocalSession>;
  listSharedLocalSessionEvents(token: string, afterSeq: number, limit?: number): Promise<LocalSessionEventPage>;
  /** Owner-only: let workspace members see this session's token meter. */
  setLocalSessionUsageSharing(sessionId: string, enabled: boolean): Promise<LocalSession>;
  /** The owner's "Let teammates pick up this session". Off deletes the stored snapshot. */
  setLocalSessionPickup(sessionId: string, enabled: boolean): Promise<LocalSession>;
  /** Short-lived links to a session's snapshot, for the Mac that will continue it. Safe to repeat with the same id. */
  requestLocalSessionPickup(sessionId: string, pickupId: string): Promise<LocalSessionPickupTicket>;
  reportLocalSessionPickupOutcome(sessionId: string, pickupId: string, outcome: LocalSessionPickupOutcome): Promise<void>;
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
  /** A short-lived presigned URL -- how a video plays (a `<video src>` carries no bearer). */
  attachmentUrl(roomId: string, attachmentId: string): Promise<{ url: string; expiresIn: number }>;
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
  /** The Activity feed, newest first; Paperclip items ride the first page only. */
  listActivity(options?: { cursor?: string | null; limit?: number }): Promise<ActivityPage>;
  /** Monotonic seen cursor; "mark all read" is `upTo: new Date().toISOString()`. */
  markActivitySeen(upTo: string): Promise<ActivitySeenResponse>;
  /** Run one of an item's non-navigate actions; resolves to the updated item. */
  runActivityAction(itemId: string, actionId: string, request?: ActivityActionRequest): Promise<ActivityItem>;
}

export function createMessagingClient(api: ApiMethods): MessagingClient {
  return {
    listWorkspaceSessions: (options) =>
      api.get(`/messaging/sessions${options?.includeEnded ? "?includeEnded=true" : ""}`) as Promise<WorkspaceSessions>,
    getLocalSession: (sessionId) => api.get(sessionPath(sessionId)) as Promise<LocalSession>,
    setLocalSessionDisplayTitle: (sessionId, displayTitle) => api.patch(`${sessionPath(sessionId)}/display-title`, { displayTitle }) as Promise<LocalSession>,
    getPrivateSendApproval: (sessionId) => api.get(`${sessionPath(sessionId)}/private-send-approval`) as Promise<PrivateSendApproval>,
    answerNativeSendApproval: (sessionId, decision) => api.post(`${sessionPath(sessionId)}/native-send-approval`, decision) as Promise<NativeSendReceipt>,
    getNativeSendReceipt: (sessionId, requestId) => api.get(`${sessionPath(sessionId)}/native-send-receipts/${encodeURIComponent(requestId)}`) as Promise<NativeSendReceipt>,
    listLocalSessionEvents: (sessionId, afterSeq, limit = 50) => api.get(
      `${sessionPath(sessionId)}/events?afterSeq=${encodeURIComponent(String(afterSeq))}&limit=${encodeURIComponent(String(limit))}`,
    ) as Promise<LocalSessionEventPage>,
    submitLocalSessionCommand: (sessionId, request, replacesRequestId) => api.post(`${sessionPath(sessionId)}/commands${replacesRequestId ? `?replacesRequestId=${encodeURIComponent(replacesRequestId)}` : ""}`, request) as Promise<LocalSessionCommandReceipt>,
    getLocalSessionCommand: (sessionId, requestId) => api.get(`${sessionPath(sessionId)}/commands/${encodeURIComponent(requestId)}`) as Promise<LocalSessionCommandReceipt>,
    cancelLocalSessionCommand: (sessionId, requestId) => api.post(`${sessionPath(sessionId)}/commands/${encodeURIComponent(requestId)}/cancel`, {}) as Promise<LocalSessionCommandReceipt>,
    retrySessionSend: (roomId, messageId, sessionId, request) => api.post(`${roomPath(roomId)}/messages/${encodeURIComponent(messageId)}/session-sends/${encodeURIComponent(sessionId)}/retry`, request) as Promise<CanonicalRoomMessage>,
    cancelSessionSend: (roomId, messageId, sessionId, request) => api.post(`${roomPath(roomId)}/messages/${encodeURIComponent(messageId)}/session-sends/${encodeURIComponent(sessionId)}/cancel`, request) as Promise<CanonicalRoomMessage>,
    listLocalSessionOutbox: (sessionId) => api.get(`${sessionPath(sessionId)}/outbox`) as Promise<LocalSessionOfflineOutbox>,
    setLocalSessionSharing: (sessionId, shared) => api.put(`${sessionPath(sessionId)}/sharing`, { shared }) as Promise<LocalSession>,
    setLocalSessionTeamControl: (sessionId, enabled) => api.put(`${sessionPath(sessionId)}/team-control`, { enabled }) as Promise<LocalSession>,
    requestLocalSessionJoin: (sessionId) => api.post(`${sessionPath(sessionId)}/join-requests`, {}) as Promise<LocalSession>,
    decideLocalSessionJoin: (sessionId, userId, decision, requestId) =>
      api.post(`${sessionPath(sessionId)}/join-requests/${encodeURIComponent(userId)}`, { decision, ...(requestId ? { requestId } : {}) }) as Promise<LocalSession>,
    removeLocalSessionSender: (sessionId, userId) =>
      api.del(`${sessionPath(sessionId)}/senders/${encodeURIComponent(userId)}`) as Promise<LocalSession>,
    createLocalSessionShareLink: (sessionId, expiresInDays) => api.post(`${sessionPath(sessionId)}/share-links`, { expiresInDays }) as Promise<LocalSessionShareLink>,
    listLocalSessionShareLinks: (sessionId) => api.get(`${sessionPath(sessionId)}/share-links`) as Promise<{ links: LocalSessionShareLink[] }>,
    revokeLocalSessionShareLink: async (sessionId, linkId) => { await api.del(`${sessionPath(sessionId)}/share-links/${encodeURIComponent(linkId)}`); },
    // POST: the token rides in the body, never a URL a log would keep.
    getSharedLocalSession: (token) => api.post("/messaging/shared-sessions/open", { token }) as Promise<SharedLocalSession>,
    listSharedLocalSessionEvents: (token, afterSeq, limit = 100) =>
      api.post("/messaging/shared-sessions/events", { token, afterSeq, limit }) as Promise<LocalSessionEventPage>,
    setLocalSessionUsageSharing: (sessionId, enabled) => api.put(`${sessionPath(sessionId)}/usage-sharing`, { enabled }) as Promise<LocalSession>,
    setLocalSessionPickup: (sessionId, enabled) => api.put(`${sessionPath(sessionId)}/pickup`, { enabled }) as Promise<LocalSession>,
    requestLocalSessionPickup: (sessionId, pickupId) => api.post(`${sessionPath(sessionId)}/pickups`, { pickupId }) as Promise<LocalSessionPickupTicket>,
    reportLocalSessionPickupOutcome: async (sessionId, pickupId, outcome) => { await api.post(`${sessionPath(sessionId)}/pickups/${encodeURIComponent(pickupId)}/outcome`, outcome); },
    listLocalSessionAudit: (sessionId) => api.get(`${sessionPath(sessionId)}/audit`) as Promise<{ entries: LocalSessionAuditEntry[] }>,
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
    attachmentUrl: (roomId, attachmentId) => api.get(`${roomPath(roomId)}/attachments/${encodeURIComponent(attachmentId)}/url`) as Promise<{ url: string; expiresIn: number }>,
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
    listActivity: (options) => {
      const query = new URLSearchParams();
      if (options?.cursor) query.set("cursor", options.cursor);
      if (options?.limit !== undefined) query.set("limit", String(options.limit));
      const qs = query.toString();
      return api.get(`/messaging/activity${qs ? `?${qs}` : ""}`) as Promise<ActivityPage>;
    },
    markActivitySeen: (upTo) => api.post("/messaging/activity/seen", { upTo }) as Promise<ActivitySeenResponse>,
    runActivityAction: (itemId, actionId, request = {}) => api.post(
      `/messaging/activity/${encodeURIComponent(itemId)}/actions/${encodeURIComponent(actionId)}`,
      request,
    ) as Promise<ActivityItem>,
  };
}
