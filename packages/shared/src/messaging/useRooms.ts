/**
 * The room state machine shared by the web and mobile clients: room list,
 * active-room detail, history reconciliation, optimistic sends, typing, live
 * runs, and read marking. Every platform-specific capability it needs —
 * identity, the live-events context value, visibility, uuid minting — arrives
 * through `RoomsPlatform`; there is no DOM global in here.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { ApiError } from "../api/error";
import { accumulateStreamText } from "../live/streamText";
import type { LiveEvent } from "../live/types";
import type { MessagingClient, RoomAttachmentInput } from "./client";
import { lastSequence, mergeCanonicalMessages, type LiveRoomRun } from "./timeline";
import type {
  CanonicalRoomMessage,
  CreateMessageRequest,
  UpdateMessageRequest,
  CreateRoomRequest,
  MemberPrefsRequest,
  MessagePage,
  MemberRef,
  PatchRoomRequest,
  RoomMember,
  RoomDetail,
  RoomSection,
  UpsertRoomMemberRequest,
} from "./types";

export type SendState = "idle" | "sending" | "persisted" | "uncertain" | "rejected";

export interface OptimisticRoomMessage extends CreateMessageRequest {
  roomId: string;
  createdAt: string;
}

export interface TypingMember {
  userId: string;
  displayName: string;
  expiresAt: string;
}

export type { LiveRoomRun } from "./timeline";

/** The live-events context value both apps already build (web and mobile
 *  providers are structurally identical over these seven fields). */
export interface RoomsLiveSource {
  messaging: MessagingClient;
  onEvent(handler: (event: LiveEvent) => void): () => void;
  claimActiveRoomSubscription(room: { roomId: string; deliveryKey: string }): () => void;
  liveChannels: readonly string[];
  connectionRevision: number;
  liveConnected: boolean;
  httpReady: boolean;
}

export interface RoomsPlatform {
  /** Signed-in user id, or null while identity is loading / signed out. */
  userId: string | null;
  /** Active organisation, or null for a personal workspace. */
  orgId: string | null;
  /**
   * The live-events context value. MUST be referentially stable across
   * renders, and so must everything on it: `messaging`, `onEvent`,
   * `claimActiveRoomSubscription`, `liveChannels`. Each is a dependency of an
   * effect inside the hook; a fresh object per render does not error, it
   * re-fires the history reconcile into "Maximum update depth exceeded".
   * Both apps' providers memoize their context value; a platform adapter
   * must pass it through, never rebuild it.
   */
  live: RoomsLiveSource;
  /** True when this room surface is on screen AND the app is foregrounded.
   *  Web: document visible + window focused. Mobile: AppState active. */
  isVisible(): boolean;
  /** Subscribe to changes in `isVisible()`. MUST be referentially stable, as must `isVisible`. */
  onVisibilityChange(listener: () => void): () => void;
  /** RFC-4122 v4. Web: crypto.randomUUID. Mobile: expo-crypto randomUUID. */
  uuid(): string;
}

export interface UseRoomsOptions {
  initialAgentId?: string | null;
  subscribeToRoom?: boolean;
  /** Select rooms[0] when nothing is active (the web sidebar's behaviour). */
  autoSelect?: boolean;
}

export type UseRoomsResult = ReturnType<typeof useRooms>;

const NO_ROOMS: never[] = [];
const NO_SECTIONS: RoomSection[] = [];
const HISTORY_FAILED = "history could not be loaded";
const SELECT_FAILED = "this conversation could not be opened";

const failureMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message ? error.message : fallback;

export function useRooms(platform: RoomsPlatform, options: UseRoomsOptions = {}) {
  const { initialAgentId = null, subscribeToRoom = true, autoSelect = true } = options;
  const { userId, orgId, live, isVisible, onVisibilityChange, uuid } = platform;
  const {
    messaging,
    claimActiveRoomSubscription,
    onEvent,
    liveChannels,
    connectionRevision,
    liveConnected,
    httpReady,
  } = live;
  const scope = userId ? `${userId}:${orgId ?? userId}` : null;
  const roomsResponse = useSWR(
    scope ? ["messaging-rooms", scope] : null,
    () => messaging.listRooms(),
    { revalidateOnFocus: false },
  );
  const roomsData = roomsResponse.data;
  const mutateRooms = roomsResponse.mutate;
  useEffect(() => {
    if (liveConnected) void mutateRooms();
  }, [connectionRevision, liveConnected, mutateRooms]);
  const rooms = roomsData?.rooms ?? NO_ROOMS;
  const sections = roomsData?.sections ?? NO_SECTIONS;
  const directoryDetailCache = useRef(new Map<string, RoomDetail>());
  const getCachedDirectoryRoomDetails = useCallback(() => {
    const details = new Map<string, RoomDetail>();
    for (const room of rooms) {
      if (room.kind === "channel") continue;
      const detail = directoryDetailCache.current.get(`${room.roomId}:${room.roomRevision}`);
      if (detail) details.set(detail.roomId, detail);
    }
    return details;
  }, [rooms]);
  const directRoomSignature = rooms
    .filter((room) => room.kind !== "channel")
    .map((room) => `${room.roomId}:${room.roomRevision}`)
    .join("|");
  const loadDirectoryRoomDetails = useCallback(async (): Promise<Map<string, RoomDetail>> => {
    // A cold agent shortcut must await the list, not interpret loading as no DMs.
    const listed = roomsData ?? await mutateRooms();
    if (!listed) throw new Error("Conversations are not ready.");
    const directRooms = listed.rooms.filter((room) => room.kind !== "channel");
    const results = await Promise.allSettled(directRooms.map((room) => messaging.getRoom(room.roomId)));
    const details = new Map<string, RoomDetail>();
    results.forEach((result, index) => {
      if (result.status !== "fulfilled") return;
      const room = directRooms[index];
      directoryDetailCache.current.set(`${room.roomId}:${room.roomRevision}`, result.value);
      details.set(result.value.roomId, result.value);
    });
    const failed = results.find((result) => result.status === "rejected");
    if (failed?.status === "rejected") throw failed.reason;
    return details;
  }, [messaging, roomsData, mutateRooms]);
  const directoryDetailsResponse = useSWR(
    scope && roomsData ? ["messaging-room-directory", scope, directRoomSignature] : null,
    loadDirectoryRoomDetails,
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
  const directoryRoomDetails: ReadonlyMap<string, RoomDetail> = directoryDetailsResponse.data ?? getCachedDirectoryRoomDetails();
  const directoryError = directoryDetailsResponse.error as Error | undefined;
  const [activeRoomId, setActiveRoomId] = useState<string | null>(null);
  const manuallyUnreadRoom = useRef<string | null>(null);
  const pendingRead = useRef<Promise<void> | null>(null);
  const dmRequests = useRef(new Map<string, string>());
  const handledInitialAgent = useRef<string | null>(null);
  const selectionRequest = useRef(0);
  const [accessRemoved, setAccessRemoved] = useState(false);
  // `selectAgentDm` is fired from an effect whose only caller is a `.catch`, so
  // a failed resolve-or-create used to vanish and leave the surface on a
  // spinner. Cleared by the next select or create that succeeds.
  const [selectRoomError, setSelectRoomError] = useState<string | null>(null);
  const [deliveryOverride, setDeliveryOverride] = useState<{ roomId: string; deliveryKey: string; roomRevision: number } | null>(null);
  const detailResponse = useSWR(
    scope && activeRoomId ? ["messaging-room", scope, activeRoomId] : null,
    () => messaging.getRoom(activeRoomId!),
    {
      revalidateOnFocus: false,
      refreshInterval: activeRoomId ? 60_000 : 0,
      shouldRetryOnError: false,
    },
  );
  const activeRoom = detailResponse.data;
  const roomError = detailResponse.error;
  const mutateRoom = detailResponse.mutate;
  useEffect(() => {
    if (manuallyUnreadRoom.current && manuallyUnreadRoom.current !== activeRoomId) {
      manuallyUnreadRoom.current = null;
    }
  }, [activeRoomId]);
  const [messages, setMessages] = useState<CanonicalRoomMessage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyComplete, setHistoryComplete] = useState(false);
  // One state, two shapes on the surface: `historyError` stays a boolean for
  // the web timeline/search controls, `historyErrorMessage` carries the reason
  // so a client can name the failure instead of saying "something went wrong".
  const [historyErrorMessage, setHistoryErrorMessage] = useState<string | null>(null);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const [optimistic, setOptimistic] = useState<OptimisticRoomMessage[]>([]);
  const [attempts, setAttempts] = useState(new Map<string, SendState>());
  const requests = useRef(new Map<string, CreateMessageRequest>());
  const [typing, setTyping] = useState(new Map<string, TypingMember>());
  const [runs, setRuns] = useState(new Map<string, LiveRoomRun>());
  const runsRef = useRef(runs);
  runsRef.current = runs;
  const generation = useRef(0);
  const requestedAfter = useRef<number | null>(null);
  const reconciliation = useRef<Promise<void> | null>(null);

  useEffect(() => {
    generation.current += 1;
    messagesRef.current = [];
    setMessages([]);
    setHistoryLoading(false);
    setHistoryComplete(false);
    setHistoryErrorMessage(null);
    setOptimistic([]);
    setAttempts(new Map());
    requests.current.clear();
    setTyping(new Map());
    setRuns(new Map());
    setAccessRemoved(false);
    setDeliveryOverride(null);
    requestedAfter.current = null;
    reconciliation.current = null;
  }, [scope, activeRoomId]);

  useEffect(() => {
    const error = roomError;
    if (!(error instanceof ApiError) || (error.status !== 403 && error.status !== 404)) return;
    setAccessRemoved(true);
    messagesRef.current = [];
    setMessages([]);
  }, [roomError]);

  const subscription = activeRoom && deliveryOverride?.roomId === activeRoom.roomId && deliveryOverride.roomRevision >= activeRoom.roomRevision
    ? { roomId: activeRoom.roomId, deliveryKey: deliveryOverride.deliveryKey }
    : activeRoom ? { roomId: activeRoom.roomId, deliveryKey: activeRoom.deliveryKey } : null;
  const subscriptionPath = subscription ? `/rooms/${subscription.roomId}/${subscription.deliveryKey}` : null;
  const subscriptionRoomId = subscription?.roomId ?? null;
  const subscriptionDeliveryKey = subscription?.deliveryKey ?? null;

  useEffect(() => {
    if (!subscribeToRoom || accessRemoved || !subscriptionRoomId || !subscriptionDeliveryKey) return;
    return claimActiveRoomSubscription({
      roomId: subscriptionRoomId,
      deliveryKey: subscriptionDeliveryKey,
    });
  }, [claimActiveRoomSubscription, subscriptionRoomId, subscriptionDeliveryKey, accessRemoved, subscribeToRoom]);

  const reconcile = useCallback((afterSeq: number) => {
    if (!activeRoomId || accessRemoved) return Promise.resolve();
    requestedAfter.current = requestedAfter.current === null ? afterSeq : Math.min(requestedAfter.current, afterSeq);
    if (reconciliation.current) return reconciliation.current;
    const roomId = activeRoomId;
    const runGeneration = generation.current;
    setHistoryLoading(true);
    setHistoryErrorMessage(null);
    const run = async () => {
      while (requestedAfter.current !== null && generation.current === runGeneration) {
        let cursor = requestedAfter.current;
        const completeHistory = cursor === 0;
        requestedAfter.current = null;
        for (;;) {
          const page: MessagePage = await messaging.listMessages(roomId, cursor, 100);
          if (generation.current !== runGeneration) return;
          const valid = page.messages.filter((message) => message.roomId === roomId);
          const merged = mergeCanonicalMessages(messagesRef.current, valid);
          messagesRef.current = merged;
          setMessages(merged);
          if (page.nextAfterSeq === null) {
            if (completeHistory) setHistoryComplete(true);
            break;
          }
          cursor = page.nextAfterSeq;
        }
      }
    };
    reconciliation.current = run().catch((error) => {
      if (generation.current === runGeneration) {
        setHistoryErrorMessage(failureMessage(error, HISTORY_FAILED));
        setHistoryComplete(false);
      }
      throw error;
    }).finally(() => {
      if (generation.current !== runGeneration) return;
      setHistoryLoading(false);
      reconciliation.current = null;
      if (requestedAfter.current !== null) void reconcile(requestedAfter.current).catch(() => {});
    });
    return reconciliation.current;
  }, [activeRoomId, accessRemoved, messaging]);

  // `room.run.frame` (carrying the terminal `run_ended`) is EPHEMERAL over the
  // live channel, unlike `room.message.created` -- it has no `afterSeq` gap
  // recovery. A client that reconnects or rotates its delivery key mid-run can
  // miss that one frame and be stuck showing "Working" forever with a
  // Steer/Stop that no longer does anything useful. Ride the same
  // reconnect/rotation signal `reconcile` already uses to re-check any run
  // still marked "running" against the server's own truth.
  const reconcileRuns = useCallback(() => {
    const inFlight = [...runsRef.current.values()].filter((run) => run.state === "running");
    for (const run of inFlight) {
      void messaging.getRunStatus(run.runId).then((status) => {
        if (status.state !== "terminal") return;
        setRuns((current) => {
          const key = `${run.roomId}:${run.agentId}:${run.runId}`;
          const existing = current.get(key);
          if (!existing || existing.state !== "running") return current;
          return new Map(current).set(key, { ...existing, state: "completed" });
        });
      }).catch(() => {
        // Best-effort: a later reconnect (or a fresh frame proving the run is
        // still alive) gets another chance.
      });
    }
  }, [messaging]);

  useEffect(() => {
    if (!subscriptionPath || !liveChannels.includes(subscriptionPath)) return;
    void reconcile(0).catch(() => {});
    reconcileRuns();
  }, [subscriptionPath, activeRoom?.roomRevision, liveChannels, connectionRevision, reconcile, reconcileRuns]);

  useEffect(() => onEvent((event) => {
    if (event.type === "room.message.updated" && event.payload.roomId === activeRoomId) {
      // ponytail: reread room history on edits; add targeted reads if long rooms make this costly.
      void reconcile(0).catch(() => {});
      return;
    }
    if (event.type === "room.message.available") {
      void mutateRooms();
      return;
    }
    if (event.type === "room.access.removed" && event.payload.roomId === activeRoomId) {
      setAccessRemoved(true);
      messagesRef.current = [];
      setMessages([]);
      setOptimistic([]);
      void mutateRooms();
      return;
    }
    if (event.type === "room.subscription.rotated" && event.payload.roomId === activeRoomId) {
      setDeliveryOverride(event.payload);
      void mutateRoom();
      return;
    }
    if (event.type === "room.metadata.changed" && event.payload.roomId === activeRoomId) {
      void mutateRoom();
      return;
    }
    if (event.type === "room.member.updated") { void mutateRooms(); return; }
    if (event.type === "room.message.reactions.changed" && event.payload.roomId === activeRoomId) {
      const merged = mergeCanonicalMessages(messagesRef.current, [event.payload]);
      messagesRef.current = merged;
      setMessages(merged);
      return;
    }
    if (event.type === "room.message.created" && event.payload.roomId === activeRoomId) {
      const previous = lastSequence(messagesRef.current);
      const merged = mergeCanonicalMessages(messagesRef.current, [event.payload]);
      messagesRef.current = merged;
      setMessages(merged);
      if (event.payload.messageSeq > previous + 1) void reconcile(previous).catch(() => {});
      void mutateRooms();
      return;
    }
    if (event.type === "room.typing.changed" && event.payload.roomId === activeRoomId) {
      setTyping((current) => {
        const next = new Map(current);
        if (Date.parse(event.payload.expiresAt) <= Date.now()) next.delete(event.payload.userId);
        else next.set(event.payload.userId, event.payload);
        return next;
      });
      return;
    }
    if (event.type === "room.run.frame" && event.payload.roomId === activeRoomId) {
      const payload = event.payload;
      const key = `${payload.roomId}:${payload.agentId}:${payload.runId}`;
      setRuns((current) => {
        const previous = current.get(key);
        const frame = payload.frame;
        const state = frame.type === "run_ended"
          ? frame.outcome === "done" ? "completed" : frame.outcome === "aborted" ? "aborted" : "failed"
          : frame.type === "error" ? "failed" : previous?.state ?? "running";
        const next: LiveRoomRun = {
          roomId: payload.roomId,
          agentId: payload.agentId,
          runId: payload.runId,
          threadRootMessageId: payload.threadRootMessageId ?? previous?.threadRootMessageId ?? null,
          text: frame.type === "chunk" ? accumulateStreamText(previous?.text ?? "", frame) : previous?.text ?? "",
          thinking: frame.type === "thinking" ? accumulateStreamText(previous?.thinking ?? "", frame) : previous?.thinking ?? "",
          frames: [...(previous?.frames ?? []), frame],
          state,
        };
        return new Map(current).set(key, next);
      });
    }
  }), [activeRoomId, onEvent, mutateRoom, mutateRooms, reconcile]);

  useEffect(() => {
    if (typing.size === 0) return;
    const timer = setInterval(() => {
      setTyping((current) => {
        const next = new Map([...current].filter(([, member]) => Date.parse(member.expiresAt) > Date.now()));
        return next.size === current.size ? current : next;
      });
    }, 1_000);
    return () => clearInterval(timer);
  }, [typing.size]);

  const markVisibleRead = useCallback(() => {
    const sequence = lastSequence(messages);
    if (!subscribeToRoom || !isVisible() ||
        !activeRoom || manuallyUnreadRoom.current === activeRoom.roomId ||
        sequence <= activeRoom.lastReadSeq || accessRemoved) return;
    const request = messaging.markRead(activeRoom.roomId, { lastReadSeq: sequence }).then(() => {
        void mutateRoom();
        void mutateRooms();
      }).catch(() => {});
    const operation = pendingRead.current
      ? Promise.all([pendingRead.current, request]).then(() => {})
      : request;
    pendingRead.current = operation;
    void operation.finally(() => {
      if (pendingRead.current === operation) pendingRead.current = null;
    });
  }, [messages, activeRoom, accessRemoved, subscribeToRoom, isVisible, messaging, mutateRoom, mutateRooms]);

  useEffect(() => {
    markVisibleRead();
    return onVisibilityChange(markVisibleRead);
  }, [markVisibleRead, onVisibilityChange]);

  const sendMessage = useCallback(async (request: CreateMessageRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const roomId = activeRoomId;
    const sendGeneration = generation.current;
    requests.current.set(request.clientMessageId, request);
    setAttempts((current) => new Map(current).set(request.clientMessageId, "sending"));
    setOptimistic((current) => current.some((message) => message.clientMessageId === request.clientMessageId)
      ? current
      : [...current, { ...request, roomId, createdAt: new Date().toISOString() }]);
    try {
      const canonical = await messaging.sendMessage(roomId, request);
      if (generation.current !== sendGeneration) {
        void mutateRooms();
        return canonical;
      }
      const merged = mergeCanonicalMessages(messagesRef.current, [canonical]);
      messagesRef.current = merged;
      setMessages(merged);
      setOptimistic((current) => current.filter((message) => message.clientMessageId !== request.clientMessageId));
      setAttempts((current) => new Map(current).set(request.clientMessageId, "persisted"));
      void mutateRooms();
      return canonical;
    } catch (error) {
      if (generation.current === sendGeneration) {
        setOptimistic((current) => current.filter((message) => message.clientMessageId !== request.clientMessageId));
        setAttempts((current) => new Map(current).set(request.clientMessageId, error instanceof ApiError ? "rejected" : "uncertain"));
      }
      throw error;
    }
  }, [activeRoomId, messaging, mutateRooms]);

  const updateMessage = useCallback(async (messageId: string, request: UpdateMessageRequest) => {
    if (!activeRoomId || accessRemoved) throw new Error("No room selected.");
    const runGeneration = generation.current;
    try {
      const message = await messaging.updateMessage(activeRoomId, messageId, request);
      if (generation.current !== runGeneration || message.roomId !== activeRoomId) return;
      const merged = mergeCanonicalMessages(messagesRef.current, [message]);
      messagesRef.current = merged;
      setMessages(merged);
    } catch (error) {
      if (generation.current === runGeneration) void reconcile(0).catch(() => {});
      throw error;
    }
  }, [activeRoomId, accessRemoved, messaging, reconcile]);

  const setReaction = useCallback(async (messageId: string, emoji: string, active: boolean) => {
    if (!activeRoomId || accessRemoved) throw new Error("No room selected.");
    const runGeneration = generation.current;
    const message = await messaging.setReaction(activeRoomId, messageId, emoji, active);
    if (generation.current !== runGeneration || message.roomId !== activeRoomId) return;
    const merged = mergeCanonicalMessages(messagesRef.current, [message]);
    messagesRef.current = merged;
    setMessages(merged);
  }, [activeRoomId, accessRemoved, messaging]);

  const reconcileSend = useCallback(async (clientMessageId: string) => {
    const request = requests.current.get(clientMessageId);
    if (!request) return;
    await reconcile(lastSequence(messagesRef.current));
    return sendMessage(request);
  }, [reconcile, sendMessage]);
  const uploadAttachment = useCallback((image: RoomAttachmentInput) => {
    if (!activeRoomId) throw new Error("No room selected.");
    return messaging.uploadAttachment(activeRoomId, image);
  }, [activeRoomId, messaging]);
  const downloadAttachment = useCallback((roomId: string, attachmentId: string) => messaging.downloadAttachment(roomId, attachmentId), [messaging]);

  const selectRoom = useCallback((roomId: string) => {
    selectionRequest.current += 1;
    if (manuallyUnreadRoom.current === roomId) {
      manuallyUnreadRoom.current = null;
      markVisibleRead();
    }
    setActiveRoomId(roomId);
  }, [markVisibleRead]);

  const markUnread = useCallback(async (roomId: string, fromSeq?: number) => {
    if (activeRoomId === roomId) manuallyUnreadRoom.current = roomId;
    try {
      await pendingRead.current;
      await messaging.markUnread(roomId, fromSeq);
      await Promise.all([mutateRooms(), activeRoomId === roomId ? mutateRoom() : Promise.resolve()]);
    } catch (error) {
      if (manuallyUnreadRoom.current === roomId) manuallyUnreadRoom.current = null;
      throw error;
    }
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  /**
   * `activate: false` resolves or creates the room WITHOUT making it this
   * instance's active room. It exists for a picker (mobile's new-message sheet)
   * that hands the id to a navigation and then stays mounted: an instance left
   * holding a room it never displays keeps `detailResponse` polling it
   * (`refreshInterval: 60_000`) and reconciles its history for the rest of the
   * session, behind the screen that legitimately owns it.
   */
  const createRoom = useCallback(async (
    request: CreateRoomRequest,
    { requestId = ++selectionRequest.current, activate = true }: { requestId?: number; activate?: boolean } = {},
  ) => {
    const created = await messaging.createRoom(request);
    setSelectRoomError(null);
    await mutateRooms();
    if (activate && requestId === selectionRequest.current) setActiveRoomId(created.roomId);
    return created;
  }, [messaging, mutateRooms]);

  const resolveDm = useCallback(async (
    participant: MemberRef,
    { activate = true }: { activate?: boolean } = {},
  ): Promise<string | null> => {
    const requestId = ++selectionRequest.current;
    let details = directoryDetailsResponse.data ?? getCachedDirectoryRoomDetails();
    let hydrationError = directoryDetailsResponse.error;
    if (!directoryDetailsResponse.data && !hydrationError) {
      try {
        details = await loadDirectoryRoomDetails();
      } catch (error) {
        details = getCachedDirectoryRoomDetails();
        hydrationError = error;
      }
    }
    if (requestId !== selectionRequest.current) return null;
    const existing = [...details.values()].find(
      (detail) => detail.kind === "dm" && detail.members.some(
        (member) => member.kind === participant.kind && member.id === participant.id,
      ),
    );
    if (existing) {
      dmRequests.current.delete(`${participant.kind}:${participant.id}`);
      if (activate) setActiveRoomId(existing.roomId);
      return existing.roomId;
    }
    if (hydrationError) throw hydrationError;
    const key = `${participant.kind}:${participant.id}`;
    const clientRoomId = dmRequests.current.get(key) ?? uuid();
    dmRequests.current.set(key, clientRoomId);
    const created = await createRoom({
      clientRoomId,
      kind: "dm",
      name: null,
      members: [{ kind: participant.kind, id: participant.id, role: "member", listenMode: participant.kind === "agent" ? "active" : null }],
    }, { requestId, activate });
    dmRequests.current.delete(key);
    return requestId === selectionRequest.current ? created.roomId : null;
  }, [createRoom, directoryDetailsResponse.data, directoryDetailsResponse.error, getCachedDirectoryRoomDetails, loadDirectoryRoomDetails, uuid]);

  const selectDm = useCallback(async (
    participant: MemberRef,
    options: { activate?: boolean } = {},
  ): Promise<string | null> => {
    try {
      const roomId = await resolveDm(participant, options);
      setSelectRoomError(null);
      return roomId;
    } catch (error) {
      setSelectRoomError(failureMessage(error, SELECT_FAILED));
      throw error;
    }
  }, [resolveDm]);

  const selectAgentDm = useCallback(
    (agentId: string) => selectDm({ kind: "agent", id: agentId }),
    [selectDm],
  );

  useEffect(() => {
    if (!roomsData) return;
    if (!initialAgentId) {
      handledInitialAgent.current = null;
      if (autoSelect && !activeRoomId && rooms.length > 0) setActiveRoomId(rooms[0].roomId);
      return;
    }
    const requestKey = `${scope}:${initialAgentId}`;
    if (handledInitialAgent.current === requestKey) return;
    handledInitialAgent.current = requestKey;
    const requestId = selectionRequest.current + 1;
    void selectAgentDm(initialAgentId).catch(() => {
      if (handledInitialAgent.current === requestKey && selectionRequest.current === requestId && autoSelect && rooms.length > 0) {
        setActiveRoomId(rooms[0].roomId);
      }
    });
  }, [activeRoomId, autoSelect, initialAgentId, rooms, roomsData, scope, selectAgentDm]);

  const updateRoom = useCallback(async (request: PatchRoomRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.updateRoom(activeRoomId, request);
    await Promise.all([mutateRoom(updated, false), mutateRooms()]);
    return updated;
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const setRoomPrefs = useCallback(async (roomId: string, prefs: MemberPrefsRequest) => {
    // Optimistic so a star or a bell flips under the cursor, then reconciled.
    await mutateRooms(
      (current) => current && { ...current, rooms: current.rooms.map((room) => room.roomId === roomId ? { ...room, ...prefs, sectionId: prefs.sectionId === undefined ? room.sectionId : prefs.sectionId } : room) },
      false,
    );
    try {
      const updated = await messaging.setMemberPrefs(roomId, prefs);
      if (roomId === activeRoomId) await mutateRoom(updated, false);
    } finally {
      await mutateRooms();
    }
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const saveSections = useCallback(async (next: readonly RoomSection[]) => {
    try {
      const response = await messaging.putSections({ sections: [...next] });
      // Merge like setRoomPrefs does, rather than replacing the cache entry
      // outright: an in-flight background revalidate (the live-event and
      // reconnect effects above call `mutateRooms()` with no debounce) can
      // resolve after this write and, with a wholesale replace, silently
      // clobber the section this call just saved back to the pre-save list.
      await mutateRooms((current) => current && { ...current, sections: response.sections, rooms: response.rooms }, false);
    } finally {
      await mutateRooms();
    }
  }, [messaging, mutateRooms]);

  const leaveRoom = useCallback(async (roomId: string) => {
    await messaging.leaveRoom(roomId);
    if (roomId === activeRoomId) setActiveRoomId(null);
    await mutateRooms();
  }, [activeRoomId, messaging, mutateRooms]);

  const setPin = useCallback(async (messageId: string, pinned: boolean) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.setPin(activeRoomId, messageId, pinned);
    await mutateRoom(updated, false);
  }, [activeRoomId, messaging, mutateRoom]);

  const upsertMember = useCallback(async (request: UpsertRoomMemberRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.upsertMember(activeRoomId, request);
    await mutateRoom(updated, false);
    return updated;
  }, [activeRoomId, messaging, mutateRoom]);

  const removeMember = useCallback(async (member: Pick<RoomMember, "kind" | "id">) => {
    if (!activeRoomId) throw new Error("No room selected.");
    await messaging.removeMember(activeRoomId, member.kind, member.id);
    await Promise.all([mutateRoom(), mutateRooms()]);
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const typingSentAt = useRef(0);
  const setRoomTyping = useCallback((isTyping: boolean) => {
    if (!activeRoomId) return Promise.resolve();
    // The backend accepts one typing signal per second per room and answers
    // 429 otherwise; keystrokes arrive faster than that, so coalesce here and
    // treat a stray 429 as the no-op it is rather than an unhandled error.
    const now = Date.now();
    if (isTyping && now - typingSentAt.current < 1000) return Promise.resolve();
    typingSentAt.current = now;
    return messaging.setTyping({ roomId: activeRoomId, isTyping }).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 429) return;
      throw error;
    });
  }, [activeRoomId, messaging]);
  const abortRun = useCallback((runId: string) => messaging.abortRun(runId), [messaging]);
  const steerRun = useCallback((runId: string, text: string) => messaging.steerRun(runId, { text }), [messaging]);

  return {
    rooms,
    sections,
    roomsError: roomsResponse.error as Error | undefined,
    roomsLoading: roomsResponse.isLoading || !scope,
    directoryError,
    directoryLoading: directoryDetailsResponse.isLoading,
    activeRoomId,
    activeRoom,
    roomError: detailResponse.error as Error | undefined,
    roomLoading: detailResponse.isLoading,
    accessRemoved,
    selectRoomError,
    messages,
    historyLoading,
    historyComplete,
    historyError: historyErrorMessage !== null,
    historyErrorMessage,
    loadHistory: () => { void reconcile(0).catch(() => {}); },
    optimisticMessages: optimistic,
    typingMembers: [...typing.values()],
    // A run whose text was already persisted as a message (same runId, or --
    // when the persisted reply's runId doesn't match the run's -- the same
    // agent posting identical trimmed text) is done double-duty; drop it so
    // the completed run card doesn't render again below the real message.
    runs: [...runs.values()].filter((run) => !messages.some((message) =>
      message.runId === run.runId ||
      (run.text.trim() && message.author.kind === "agent" && message.author.id === run.agentId && message.text.trim() === run.text.trim()))),
    liveConnected,
    httpReady,
    selectRoom,
    resourceSource: { client: messaging, onEvent, connectionRevision },
    selectDm,
    selectAgentDm,
    directoryRoomDetails,
    refreshRooms: () => Promise.all([mutateRooms(), directoryDetailsResponse.mutate()]),
    refreshRoom: () => mutateRoom(),
    createRoom,
    updateRoom,
    setRoomPrefs,
    saveSections,
    leaveRoom,
    setPin,
    upsertMember,
    removeMember,
    markUnread,
    sendMessage,
    setReaction,
    updateMessage,
    uploadAttachment,
    downloadAttachment,
    reconcileSend,
    sendState: (clientMessageId: string): SendState => attempts.get(clientMessageId) ?? "idle",
    setTyping: setRoomTyping,
    abortRun,
    steerRun,
  };
}
