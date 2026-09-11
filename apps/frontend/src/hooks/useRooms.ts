"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import {
  ApiError,
  accumulateStreamText,
  type CanonicalRoomMessage,
  type ChatIncomingMessage,
  type CreateMessageRequest,
  type UpdateMessageRequest,
  type CreateRoomRequest,
  type MessagePage,
  type MemberRef,
  type PatchRoomRequest,
  type RoomMember,
  type RoomDetail,
  type UpsertRoomMemberRequest,
} from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

export type SendState = "idle" | "sending" | "persisted" | "uncertain" | "rejected";

export interface OptimisticRoomMessage extends CreateMessageRequest {
  roomId: string;
  createdAt: string;
}

export interface LiveRoomRun {
  roomId: string;
  agentId: string;
  runId: string;
  threadRootMessageId?: string | null;
  text: string;
  thinking: string;
  frames: ChatIncomingMessage[];
  state: "running" | "completed" | "failed" | "aborted";
}

export interface TypingMember {
  userId: string;
  displayName: string;
  expiresAt: string;
}

export function mergeCanonicalMessages(
  current: readonly CanonicalRoomMessage[],
  incoming: readonly CanonicalRoomMessage[],
): CanonicalRoomMessage[] {
  const byId = new Map(current.map((message) => [message.messageId, message]));
  for (const message of incoming) {
    const previous = byId.get(message.messageId);
    const content = (previous?.messageRevision ?? 0) > (message.messageRevision ?? 0) ? previous! : message;
    const reactions = (previous?.reactionRevision ?? 0) > (message.reactionRevision ?? 0) ? previous! : message;
    byId.set(message.messageId, { ...content, reactions: content.deletedAt ? {} : reactions.reactions, reactionRevision: reactions.reactionRevision });
  }
  return [...byId.values()].sort((left, right) => left.messageSeq - right.messageSeq);
}

const lastSequence = (messages: readonly CanonicalRoomMessage[]) => messages.at(-1)?.messageSeq ?? 0;
const NO_ROOMS: never[] = [];

export function useRooms(initialAgentId?: string | null, subscribeToRoom = true) {
  const { isLoaded, userId, orgId } = useAuth();
  const live = useLiveEvents();
  const {
    messaging,
    claimActiveRoomSubscription,
    onEvent,
    liveChannels,
    connectionRevision,
    liveConnected,
    httpReady,
  } = live;
  const scope = isLoaded && userId ? `${userId}:${orgId ?? userId}` : null;
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
  const dmRequests = useRef(new Map<string, string>());
  const handledInitialAgent = useRef<string | null>(null);
  const selectionRequest = useRef(0);
  const [accessRemoved, setAccessRemoved] = useState(false);
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
  const [historyError, setHistoryError] = useState(false);
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
    setHistoryError(false);
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
    setHistoryError(false);
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
        setHistoryError(true);
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

  useEffect(() => {
    const markVisibleRead = () => {
      const sequence = lastSequence(messages);
      if (!subscribeToRoom || document.visibilityState !== "visible" || !document.hasFocus() ||
          !activeRoom || manuallyUnreadRoom.current === activeRoom.roomId ||
          sequence <= activeRoom.lastReadSeq || accessRemoved) return;
      void messaging.markRead(activeRoom.roomId, { lastReadSeq: sequence }).then(() => {
        void mutateRoom();
        void mutateRooms();
      }).catch(() => {});
    };
    markVisibleRead();
    window.addEventListener("focus", markVisibleRead);
    document.addEventListener("visibilitychange", markVisibleRead);
    return () => {
      window.removeEventListener("focus", markVisibleRead);
      document.removeEventListener("visibilitychange", markVisibleRead);
    };
  }, [messages, activeRoom, accessRemoved, subscribeToRoom, messaging, mutateRoom, mutateRooms]);

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
  const uploadAttachment = useCallback((image: File) => {
    if (!activeRoomId) throw new Error("No room selected.");
    return messaging.uploadAttachment(activeRoomId, image);
  }, [activeRoomId, messaging]);
  const downloadAttachment = useCallback((roomId: string, attachmentId: string) => messaging.downloadAttachment(roomId, attachmentId), [messaging]);

  const selectRoom = useCallback((roomId: string) => {
    selectionRequest.current += 1;
    setActiveRoomId(roomId);
  }, []);

  const markUnread = useCallback(async (roomId: string) => {
    if (activeRoomId === roomId) manuallyUnreadRoom.current = roomId;
    try {
      await messaging.markUnread(roomId);
      await Promise.all([mutateRooms(), activeRoomId === roomId ? mutateRoom() : Promise.resolve()]);
    } catch (error) {
      if (manuallyUnreadRoom.current === roomId) manuallyUnreadRoom.current = null;
      throw error;
    }
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const createRoom = useCallback(async (request: CreateRoomRequest, requestId = ++selectionRequest.current) => {
    const created = await messaging.createRoom(request);
    await mutateRooms();
    if (requestId === selectionRequest.current) setActiveRoomId(created.roomId);
    return created;
  }, [messaging, mutateRooms]);

  const selectDm = useCallback(async (participant: MemberRef): Promise<string | null> => {
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
      setActiveRoomId(existing.roomId);
      return existing.roomId;
    }
    if (hydrationError) throw hydrationError;
    const key = `${participant.kind}:${participant.id}`;
    const clientRoomId = dmRequests.current.get(key) ?? crypto.randomUUID();
    dmRequests.current.set(key, clientRoomId);
    const created = await createRoom({
      clientRoomId,
      kind: "dm",
      name: null,
      members: [{ kind: participant.kind, id: participant.id, role: "member", listenMode: participant.kind === "agent" ? "active" : null }],
    }, requestId);
    dmRequests.current.delete(key);
    return requestId === selectionRequest.current ? created.roomId : null;
  }, [createRoom, directoryDetailsResponse.data, directoryDetailsResponse.error, getCachedDirectoryRoomDetails, loadDirectoryRoomDetails]);

  const selectAgentDm = useCallback(
    (agentId: string) => selectDm({ kind: "agent", id: agentId }),
    [selectDm],
  );

  useEffect(() => {
    if (!roomsData) return;
    if (!initialAgentId) {
      handledInitialAgent.current = null;
      if (!activeRoomId && rooms.length > 0) setActiveRoomId(rooms[0].roomId);
      return;
    }
    const requestKey = `${scope}:${initialAgentId}`;
    if (handledInitialAgent.current === requestKey) return;
    handledInitialAgent.current = requestKey;
    const requestId = selectionRequest.current + 1;
    void selectAgentDm(initialAgentId).catch(() => {
      if (handledInitialAgent.current === requestKey && selectionRequest.current === requestId && rooms.length > 0) {
        setActiveRoomId(rooms[0].roomId);
      }
    });
  }, [activeRoomId, initialAgentId, rooms, roomsData, scope, selectAgentDm]);

  const updateRoom = useCallback(async (request: PatchRoomRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.updateRoom(activeRoomId, request);
    await Promise.all([mutateRoom(updated, false), mutateRooms()]);
    return updated;
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

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
    roomsError: roomsResponse.error as Error | undefined,
    roomsLoading: roomsResponse.isLoading,
    directoryError,
    directoryLoading: directoryDetailsResponse.isLoading,
    activeRoomId,
    activeRoom,
    roomError: detailResponse.error as Error | undefined,
    roomLoading: detailResponse.isLoading,
    accessRemoved,
    messages,
    historyLoading,
    historyComplete,
    historyError,
    loadHistory: () => { void reconcile(0).catch(() => {}); },
    optimisticMessages: optimistic,
    typingMembers: [...typing.values()],
    runs: [...runs.values()].filter((run) => !messages.some((message) => message.runId === run.runId)),
    liveConnected,
    httpReady,
    selectRoom,
    selectDm,
    selectAgentDm,
    directoryRoomDetails,
    refreshRooms: () => Promise.all([mutateRooms(), directoryDetailsResponse.mutate()]),
    refreshRoom: () => mutateRoom(),
    createRoom,
    updateRoom,
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
