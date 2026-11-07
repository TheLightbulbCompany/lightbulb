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
  MessageSearchResponse,
  SessionSearchResponse,
  PatchRoomRequest,
  ReadCursorRequest,
  ReadCursorResponse,
  RoomMemoryResponse,
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
  LocalSessionEventPage,
  LocalSessionCommand,
  LocalSessionCommandReceipt,
  LocalSessionOfflineOutbox,
  LocalSessionAuditEntry,
  LocalSessionPickupOutcome,
  LocalSessionPickupTicket,
  SessionCopyCode,
  SessionLink,
  SessionLinkAudience,
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

const subagentPath = (roomId: string, agentId: string, subagentId: string) =>
  `/messaging/rooms/${encodeURIComponent(roomId)}/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentId)}`;

export type SubagentTail = { subagentId: string; available: boolean; text: string; truncated: boolean };

export interface MessagingClient {
  /** `includeEnded` appends sessions that ended in the last 7 days (the sidebar's "Recent"). */
  listWorkspaceSessions(options?: { includeEnded?: boolean }): Promise<WorkspaceSessions>;
  /** One session, ended included -- the list keeps only live ones. 404s LOCAL_SESSION_NOT_FOUND otherwise. */
  getLocalSession(sessionId: string): Promise<LocalSession>;
  /**
   * Owner only: take one session off every list. Its link stops and nobody can open or resume it. A running one is
   * ended on its Mac first and leaves the list when it has stopped (409 when its Mac is offline or it cannot be ended from here).
   */
  removeLocalSession(sessionId: string): Promise<void>;
  /** The session's chat room: created on the first open, the caller seated (people only). */
  openSessionChat(sessionId: string): Promise<RoomDetail>;
  /** Owner-only display name; null restores the provider title. */
  setLocalSessionDisplayTitle(sessionId: string, displayTitle: string | null): Promise<LocalSession>;
  listLocalSessionEvents(sessionId: string, afterSeq: number, limit?: number): Promise<LocalSessionEventPage>;
  submitLocalSessionCommand(sessionId: string, request: LocalSessionCommand, replacesRequestId?: string): Promise<LocalSessionCommandReceipt>;
  getLocalSessionCommand(sessionId: string, requestId: string): Promise<LocalSessionCommandReceipt>;
  cancelLocalSessionCommand(sessionId: string, requestId: string): Promise<LocalSessionCommandReceipt>;
  listLocalSessionOutbox(sessionId: string): Promise<LocalSessionOfflineOutbox>;
  /** Owner only: set one session to Only me (false) or share it again (true). */
  setLocalSessionSharing(sessionId: string, shared: boolean): Promise<LocalSession>;
  /** Owner-only: let workspace members use this session's controls. */
  setLocalSessionTeamControl(sessionId: string, enabled: boolean): Promise<LocalSession>;
  /** A teammate asks the session's owner to let them send. Repeating while pending changes nothing. */
  requestLocalSessionJoin(sessionId: string): Promise<LocalSession>;
  /** A teammate asks the owner to allow pick-up. (Not requestLocalSessionPickup: that one starts a pick-up.) */
  requestLocalSessionPickupAccess(sessionId: string): Promise<LocalSession>;
  decideLocalSessionPickup(sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string): Promise<LocalSession>;
  /** Keep a running session's live terminal coming to this person; sent every 15 s while it is on screen. */
  /** `fresh`: the view just mounted with no screen; the backend asks the Mac for one now. */
  /** Answers with how many people watch while some watch by an "Anyone" link (counts, never who); nothing from an older backend. */
  watchLocalSession(sessionId: string, fresh?: boolean): Promise<{ watching: number; viaLink: number } | undefined>;
  /** An admitted viewer's keystrokes (base64, at most 4096 raw bytes) for the live terminal screen `epoch` they are looking at.
   *  `size`: their panel's cols x rows, with keys or alone (`data` ""); `claim` when they use the panel now. The session
   *  takes the size of whoever used it last (owner ruling 2026-10-14, tmux's `window-size latest`). */
  sendLocalSessionInput(sessionId: string, data: string, epoch: number, size?: { cols: number; rows: number; claim: boolean }): Promise<void>;
  /** The owner allows or declines one person's ask. Allow lets just that person send. */
  decideLocalSessionJoin(sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string): Promise<LocalSession>;
  /** The owner takes one allowed person's access back. */
  removeLocalSessionSender(sessionId: string, userId: string): Promise<LocalSession>;
  /** "Resume and send": starts an ended session again on its owner's Mac. Safe to repeat with the same resumeId. */
  resumeLocalSession(sessionId: string, request: { resumeId: string; message?: string }): Promise<LocalSession>;
  /** The owner takes one person's resume back. */
  revokeLocalSessionResume(sessionId: string, userId: string): Promise<LocalSession>;
  listLocalSessionAudit(sessionId: string): Promise<{ entries: LocalSessionAuditEntry[] }>;
  /** Owner-only: the session's one link (404 when it has none). */
  getLocalSessionLink(sessionId: string): Promise<SessionLink>;
  /** No audience is Share: create a workspace link if there is none, otherwise keep the link's audience. */
  putLocalSessionLink(sessionId: string, audience?: SessionLinkAudience): Promise<SessionLink>;
  /** The owner's "Let people continue this session". Off keeps the link. */
  setLocalSessionPickup(sessionId: string, enabled: boolean): Promise<LocalSession>;
  /** Continue: the moment a copy is made. The challenge is the CLI's; the token rides in the body. */
  createSessionCopy(token: string, challenge: string): Promise<SessionCopyCode>;
  /** Owner-only: let workspace members see this session's token meter. */
  setLocalSessionUsageSharing(sessionId: string, enabled: boolean): Promise<LocalSession>;
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
  /** `head`: only the first N bytes (≤ 16 KB, a ranged read server-side), for a preview; `signal` cancels it. */
  downloadAttachment(roomId: string, attachmentId: string, opts?: { head?: number; signal?: AbortSignal }): Promise<Blob>;
  /** A short-lived presigned URL -- how a video plays (a `<video src>` carries no bearer). */
  attachmentUrl(roomId: string, attachmentId: string): Promise<{ url: string; expiresIn: number }>;
  markRead(roomId: string, request: ReadCursorRequest): Promise<ReadCursorResponse>;
  markUnread(roomId: string, fromSeq?: number): Promise<ReadCursorResponse>;
  setMemberPrefs(roomId: string, request: MemberPrefsRequest): Promise<RoomDetail>;
  putSections(request: SectionsRequest): Promise<RoomListResponse>;
  leaveRoom(roomId: string): Promise<void>;
  /** Owner-only channel delete (a tombstone; history is kept server-side). */
  deleteRoom(roomId: string): Promise<void>;
  searchMessages(query: string, options?: { roomId?: string; limit?: number }): Promise<MessageSearchResponse>;
  warmSearch(): Promise<void>;
  /** What was said in the coding sessions the caller may read. */
  searchSessions(query: string, options?: { limit?: number }): Promise<SessionSearchResponse>;
  setPin(roomId: string, messageId: string, pinned: boolean): Promise<RoomDetail>;
  /** What Lightbulb noted in this room, newest first. */
  listRoomMemory(roomId: string): Promise<RoomMemoryResponse>;
  /** Forget one noted fact (the room's owner or admin). */
  forgetRoomMemory(roomId: string, messageId: string): Promise<void>;
  setTyping(request: TypingRequest): Promise<void>;
  abortRun(runId: string): Promise<AbortRunResponse>;
  steerRun(runId: string, request: SteerRunRequest): Promise<SteerRunResponse>;
  listSubagents(roomId: string): Promise<{ subagents: Record<string, unknown>[] }>;
  subagentTail(roomId: string, agentId: string, subagentId: string): Promise<SubagentTail>;
  steerSubagent(roomId: string, agentId: string, subagentId: string, text: string): Promise<{ state: "queued" }>;
  stopSubagent(roomId: string, agentId: string, subagentId: string): Promise<{ state: "stopping" }>;
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
    removeLocalSession: async (sessionId) => { await api.del(sessionPath(sessionId)); },
    openSessionChat: (sessionId) => api.post(`${sessionPath(sessionId)}/chat`, {}) as Promise<RoomDetail>,
    setLocalSessionDisplayTitle: (sessionId, displayTitle) => api.patch(`${sessionPath(sessionId)}/display-title`, { displayTitle }) as Promise<LocalSession>,
    listLocalSessionEvents: (sessionId, afterSeq, limit = 50) => api.get(
      `${sessionPath(sessionId)}/events?afterSeq=${encodeURIComponent(String(afterSeq))}&limit=${encodeURIComponent(String(limit))}`,
    ) as Promise<LocalSessionEventPage>,
    submitLocalSessionCommand: (sessionId, request, replacesRequestId) => api.post(`${sessionPath(sessionId)}/commands${replacesRequestId ? `?replacesRequestId=${encodeURIComponent(replacesRequestId)}` : ""}`, request) as Promise<LocalSessionCommandReceipt>,
    getLocalSessionCommand: (sessionId, requestId) => api.get(`${sessionPath(sessionId)}/commands/${encodeURIComponent(requestId)}`) as Promise<LocalSessionCommandReceipt>,
    cancelLocalSessionCommand: (sessionId, requestId) => api.post(`${sessionPath(sessionId)}/commands/${encodeURIComponent(requestId)}/cancel`, {}) as Promise<LocalSessionCommandReceipt>,
    listLocalSessionOutbox: (sessionId) => api.get(`${sessionPath(sessionId)}/outbox`) as Promise<LocalSessionOfflineOutbox>,
    setLocalSessionSharing: (sessionId, shared) => api.put(`${sessionPath(sessionId)}/sharing`, { shared }) as Promise<LocalSession>,
    setLocalSessionTeamControl: (sessionId, enabled) => api.put(`${sessionPath(sessionId)}/team-control`, { enabled }) as Promise<LocalSession>,
    requestLocalSessionJoin: (sessionId) => api.post(`${sessionPath(sessionId)}/join-requests`, {}) as Promise<LocalSession>,
    requestLocalSessionPickupAccess: (sessionId) => api.post(`${sessionPath(sessionId)}/pickup-requests`, {}) as Promise<LocalSession>,
    decideLocalSessionPickup: (sessionId, userId, decision, requestId) =>
      api.post(`${sessionPath(sessionId)}/pickup-requests/${encodeURIComponent(userId)}`, { decision, ...(requestId ? { requestId } : {}) }) as Promise<LocalSession>,
    watchLocalSession: (sessionId, fresh) => api.post(`${sessionPath(sessionId)}/viewing`, fresh === true ? { fresh: true } : {}) as Promise<{ watching: number; viaLink: number } | undefined>,
    sendLocalSessionInput: async (sessionId, data, epoch, size) => { await api.post(`${sessionPath(sessionId)}/input`, { ...(data ? { data } : {}), epoch, ...(size ? { size } : {}) }); },
    decideLocalSessionJoin: (sessionId, userId, decision, requestId) =>
      api.post(`${sessionPath(sessionId)}/join-requests/${encodeURIComponent(userId)}`, { decision, ...(requestId ? { requestId } : {}) }) as Promise<LocalSession>,
    removeLocalSessionSender: (sessionId, userId) =>
      api.del(`${sessionPath(sessionId)}/senders/${encodeURIComponent(userId)}`) as Promise<LocalSession>,
    resumeLocalSession: (sessionId, request) => api.post(`${sessionPath(sessionId)}/resume`, request) as Promise<LocalSession>,
    revokeLocalSessionResume: (sessionId, userId) =>
      api.del(`${sessionPath(sessionId)}/resume-allowed/${encodeURIComponent(userId)}`) as Promise<LocalSession>,
    getLocalSessionLink: (sessionId) => api.get(`${sessionPath(sessionId)}/link`) as Promise<SessionLink>,
    putLocalSessionLink: (sessionId, audience) => api.put(`${sessionPath(sessionId)}/link`, { audience }) as Promise<SessionLink>,
    setLocalSessionPickup: (sessionId, enabled) => api.put(`${sessionPath(sessionId)}/pickup`, { enabled }) as Promise<LocalSession>,
    // POST: the token rides in the body, never a URL a log would keep.
    createSessionCopy: (token, challenge) => api.post("/session-links/copies", { token, challenge }) as Promise<SessionCopyCode>,
    setLocalSessionUsageSharing: (sessionId, enabled) => api.put(`${sessionPath(sessionId)}/usage-sharing`, { enabled }) as Promise<LocalSession>,
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
    downloadAttachment: (roomId, attachmentId, opts) => api.downloadBlob(
      `${roomPath(roomId)}/attachments/${encodeURIComponent(attachmentId)}${opts?.head ? `?head=${opts.head}` : ""}`,
      opts?.signal ? { signal: opts.signal } : undefined),
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
    // POST: the query must not ride in a URL (access logs).
    searchMessages: (query, options) => api.post("/messaging/search", {
      q: query,
      ...(options?.roomId ? { roomId: options.roomId } : {}),
      ...(options?.limit ? { limit: options.limit } : {}),
    }) as Promise<MessageSearchResponse>,
    async warmSearch() {
      await api.post("/messaging/search/warm", {});
    },
    searchSessions: (query, options) => api.post("/messaging/sessions/search", {
      q: query,
      ...(options?.limit ? { limit: options.limit } : {}),
    }) as Promise<SessionSearchResponse>,
    setPin: (roomId, messageId, pinned) => (pinned
      ? api.put(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`, {})
      : api.del(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`)) as Promise<RoomDetail>,
    listRoomMemory: (roomId) => api.get(`${roomPath(roomId)}/memory`) as Promise<RoomMemoryResponse>,
    async forgetRoomMemory(roomId, messageId) {
      await api.del(`${roomPath(roomId)}/memory/${encodeURIComponent(messageId)}`);
    },
    async setTyping(request) {
      await api.post("/messaging/typing", request);
    },
    abortRun: (runId) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/abort`, {}) as Promise<AbortRunResponse>,
    steerRun: (runId, request) => api.post(`/messaging/runs/${encodeURIComponent(runId)}/steer`, request) as Promise<SteerRunResponse>,
    listSubagents: (roomId) => api.get(`/messaging/rooms/${encodeURIComponent(roomId)}/subagents`) as Promise<{ subagents: Record<string, unknown>[] }>,
    subagentTail: (roomId, agentId, subagentId) => api.get(`${subagentPath(roomId, agentId, subagentId)}/tail`) as Promise<SubagentTail>,
    steerSubagent: (roomId, agentId, subagentId, text) => api.post(`${subagentPath(roomId, agentId, subagentId)}/steer`, { text }) as Promise<{ state: "queued" }>,
    stopSubagent: (roomId, agentId, subagentId) => api.post(`${subagentPath(roomId, agentId, subagentId)}/stop`, {}) as Promise<{ state: "stopping" }>,
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
