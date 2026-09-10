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
  type CreateRoomRequest,
  type MessagePage,
  type PatchRoomRequest,
  type RoomMember,
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
  for (const message of incoming) byId.set(message.messageId, message);
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
  const rooms = roomsData?.rooms ?? NO_ROOMS;
  const [activeRoomId, setActiveRoomId] = useState<string | null>(null);
  const initialDmRequest = useRef<{ agentId: string; clientRoomId: string } | null>(null);
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
  const [messages, setMessages] = useState<CanonicalRoomMessage[]>([]);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const [optimistic, setOptimistic] = useState<OptimisticRoomMessage[]>([]);
  const [attempts, setAttempts] = useState(new Map<string, SendState>());
  const requests = useRef(new Map<string, CreateMessageRequest>());
  const [typing, setTyping] = useState(new Map<string, TypingMember>());
  const [runs, setRuns] = useState(new Map<string, LiveRoomRun>());
  const generation = useRef(0);
  const requestedAfter = useRef<number | null>(null);
  const reconciliation = useRef<Promise<void> | null>(null);

  useEffect(() => {
    if (!roomsData || activeRoomId) return;
    let cancelled = false;
    if (!initialAgentId) {
      if (rooms.length > 0) setActiveRoomId(rooms[0].roomId);
      return;
    }
    void Promise.all(rooms
      .filter((room) => room.kind === "dm")
      .map((room) => messaging.getRoom(room.roomId).catch(() => null)))
      .then(async (details) => {
        const dm = details.find((detail) => detail?.members.some(
          (member) => member.kind === "agent" && member.id === initialAgentId,
        ));
        if (dm) return dm;
        const creation = initialDmRequest.current?.agentId === initialAgentId
          ? initialDmRequest.current
          : { agentId: initialAgentId, clientRoomId: crypto.randomUUID() };
        initialDmRequest.current = creation;
        return messaging.createRoom({
          clientRoomId: creation.clientRoomId,
          kind: "dm",
          name: null,
          members: [{ kind: "agent", id: initialAgentId, role: "member", listenMode: "active" }],
        });
      })
      .then((dm) => {
        if (cancelled) return;
        setActiveRoomId(dm.roomId);
        void mutateRooms();
      })
      .catch(() => {
        if (!cancelled && rooms.length > 0) setActiveRoomId(rooms[0].roomId);
      });
    return () => {
      cancelled = true;
    };
  }, [roomsData, rooms, activeRoomId, initialAgentId, messaging, mutateRooms]);

  useEffect(() => {
    generation.current += 1;
    messagesRef.current = [];
    setMessages([]);
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
    const run = async () => {
      while (requestedAfter.current !== null && generation.current === runGeneration) {
        let cursor = requestedAfter.current;
        requestedAfter.current = null;
        for (;;) {
          const page: MessagePage = await messaging.listMessages(roomId, cursor, 100);
          if (generation.current !== runGeneration) return;
          const valid = page.messages.filter((message) => message.roomId === roomId);
          const merged = mergeCanonicalMessages(messagesRef.current, valid);
          messagesRef.current = merged;
          setMessages(merged);
          if (page.nextAfterSeq === null) break;
          cursor = page.nextAfterSeq;
        }
      }
    };
    reconciliation.current = run().finally(() => {
      reconciliation.current = null;
      if (requestedAfter.current !== null && generation.current === runGeneration) void reconcile(requestedAfter.current);
    });
    return reconciliation.current;
  }, [activeRoomId, accessRemoved, messaging]);

  useEffect(() => {
    if (!subscriptionPath || !liveChannels.includes(subscriptionPath)) return;
    void reconcile(lastSequence(messagesRef.current));
  }, [subscriptionPath, activeRoom?.roomRevision, liveChannels, connectionRevision, reconcile]);

  useEffect(() => onEvent((event) => {
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
    if (event.type === "room.message.created" && event.payload.roomId === activeRoomId) {
      const previous = lastSequence(messagesRef.current);
      const merged = mergeCanonicalMessages(messagesRef.current, [event.payload]);
      messagesRef.current = merged;
      setMessages(merged);
      if (event.payload.messageSeq > previous + 1) void reconcile(previous);
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
    const sequence = lastSequence(messages);
    if (!activeRoom || sequence <= activeRoom.lastReadSeq || accessRemoved) return;
    void messaging.markRead(activeRoom.roomId, { lastReadSeq: sequence }).then(() => {
      void mutateRoom();
      void mutateRooms();
    }).catch(() => {});
  }, [messages, activeRoom, accessRemoved, messaging, mutateRoom, mutateRooms]);

  const sendMessage = useCallback(async (request: CreateMessageRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    requests.current.set(request.clientMessageId, request);
    setAttempts((current) => new Map(current).set(request.clientMessageId, "sending"));
    setOptimistic((current) => current.some((message) => message.clientMessageId === request.clientMessageId)
      ? current
      : [...current, { ...request, roomId: activeRoomId, createdAt: new Date().toISOString() }]);
    try {
      const canonical = await messaging.sendMessage(activeRoomId, request);
      const merged = mergeCanonicalMessages(messagesRef.current, [canonical]);
      messagesRef.current = merged;
      setMessages(merged);
      setOptimistic((current) => current.filter((message) => message.clientMessageId !== request.clientMessageId));
      setAttempts((current) => new Map(current).set(request.clientMessageId, "persisted"));
      void mutateRooms();
      return canonical;
    } catch (error) {
      setAttempts((current) => new Map(current).set(request.clientMessageId, error instanceof ApiError ? "rejected" : "uncertain"));
      throw error;
    }
  }, [activeRoomId, messaging, mutateRooms]);

  const reconcileSend = useCallback(async (clientMessageId: string) => {
    const request = requests.current.get(clientMessageId);
    if (!request) return;
    await reconcile(lastSequence(messagesRef.current));
    return sendMessage(request);
  }, [reconcile, sendMessage]);

  const selectRoom = useCallback((roomId: string) => {
    setActiveRoomId(roomId);
  }, []);

  const createRoom = useCallback(async (request: CreateRoomRequest) => {
    const created = await messaging.createRoom(request);
    await mutateRooms();
    setActiveRoomId(created.roomId);
    return created;
  }, [messaging, mutateRooms]);

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

  const setRoomTyping = useCallback((isTyping: boolean) => activeRoomId
    ? messaging.setTyping({ roomId: activeRoomId, isTyping })
    : Promise.resolve(), [activeRoomId, messaging]);
  const abortRun = useCallback((runId: string) => messaging.abortRun(runId), [messaging]);
  const steerRun = useCallback((runId: string, text: string) => messaging.steerRun(runId, { text }), [messaging]);

  return {
    rooms,
    roomsError: roomsResponse.error as Error | undefined,
    roomsLoading: roomsResponse.isLoading,
    activeRoomId,
    activeRoom,
    roomError: detailResponse.error as Error | undefined,
    roomLoading: detailResponse.isLoading,
    accessRemoved,
    messages,
    optimisticMessages: optimistic,
    typingMembers: [...typing.values()],
    runs: [...runs.values()].filter((run) => !messages.some((message) => message.runId === run.runId)),
    liveConnected,
    httpReady,
    selectRoom,
    refreshRooms: () => mutateRooms(),
    refreshRoom: () => mutateRoom(),
    createRoom,
    updateRoom,
    upsertMember,
    removeMember,
    sendMessage,
    reconcileSend,
    sendState: (clientMessageId: string): SendState => attempts.get(clientMessageId) ?? "idle",
    setTyping: setRoomTyping,
    abortRun,
    steerRun,
  };
}
