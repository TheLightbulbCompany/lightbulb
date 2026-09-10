"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatIncomingMessage, RoomDetail } from "@lightbulb/shared";
import { RuntimeRpcError } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

/**
 * Narrow compatibility boundary for the established chat reducer. Commands are
 * canonical HTTP writes; only run frames arrive through the active AppSync room
 * subscription.
 */
export function useAgentRoomTransport(agentId: string | null) {
  const {
    httpReady,
    liveConnected,
    messaging,
    runtime,
    setActiveRoomSubscription,
    onChatMessage: subscribeChatMessage,
  } = useLiveEvents();
  const [roomState, setRoomState] = useState<{ agentId: string; room: RoomDetail } | null>(null);
  const room = roomState?.agentId === agentId ? roomState.room : null;
  const roomRef = useRef<RoomDetail | null>(null);
  const roomCreationRef = useRef<{ agentId: string; clientRoomId: string } | null>(null);
  const lookupRef = useRef<{ agentId: string; promise: Promise<RoomDetail> } | null>(null);

  const resolveRoom = useCallback((): Promise<RoomDetail> => {
    if (!agentId) return Promise.reject(new Error("No agent selected"));
    if (roomRef.current?.members.some((member) => member.kind === "agent" && member.id === agentId)) {
      return Promise.resolve(roomRef.current);
    }
    if (lookupRef.current?.agentId === agentId) return lookupRef.current.promise;

    const promise = messaging.listRooms().then(async ({ rooms }) => {
      const details = await Promise.all(rooms
        .filter((summary) => summary.kind === "dm")
        .map((summary) => messaging.getRoom(summary.roomId).catch(() => null)));
      const existing = details.find((detail): detail is RoomDetail => !!detail && detail.members.some(
        (member) => member.kind === "agent" && member.id === agentId,
      ));
      if (existing) return existing;
      const creation = roomCreationRef.current?.agentId === agentId
        ? roomCreationRef.current
        : { agentId, clientRoomId: crypto.randomUUID() };
      roomCreationRef.current = creation;
      return messaging.createRoom({
        clientRoomId: creation.clientRoomId,
        kind: "dm",
        name: null,
        members: [{ kind: "agent", id: agentId, role: "member", listenMode: "active" }],
      });
    });
    lookupRef.current = { agentId, promise };
    void promise.then((detail) => {
      if (lookupRef.current?.promise !== promise) return;
      roomRef.current = detail;
      setRoomState({ agentId, room: detail });
    }).catch(() => {
      if (lookupRef.current?.promise === promise) lookupRef.current = null;
    });
    return promise;
  }, [agentId, messaging]);

  useEffect(() => {
    roomRef.current = null;
    lookupRef.current = null;
    if (agentId && httpReady) void resolveRoom().catch(() => {});
  }, [agentId, httpReady, resolveRoom]);

  useEffect(() => {
    if (!room) return;
    setActiveRoomSubscription({ roomId: room.roomId, deliveryKey: room.deliveryKey });
    return () => setActiveRoomSubscription(null);
  }, [room, setActiveRoomSubscription]);

  const onChatMessage = useCallback((handler: (frame: ChatIncomingMessage) => void) => subscribeChatMessage((frame, event) => {
    if (event.payload.agentId !== agentId || event.payload.roomId !== roomRef.current?.roomId) return;
    handler(frame);
  }), [agentId, subscribeChatMessage]);

  const sendRoomMessage = useCallback(async (text: string, clientMessageId: string) => {
    const target = await resolveRoom();
    return messaging.sendMessage(target.roomId, {
      clientMessageId,
      text,
      mentions: [],
      replyToMessageId: null,
      threadRootMessageId: null,
    });
  }, [messaging, resolveRoom]);

  const rpc = useCallback(async <T,>(method: string, params?: Record<string, unknown>): Promise<T> => {
    const outcome = await runtime.rpc<T>(method, params);
    if (outcome.kind === "result") return outcome.value;
    throw new RuntimeRpcError({
      status: 202,
      code: "OPERATION_PENDING",
      message: "The operation is still running.",
      retryable: true,
      details: { operationId: outcome.operationId },
    });
  }, [runtime]);

  return {
    // Sending is an authenticated HTTP capability and must not depend on
    // AppSync's transient connection state.
    isConnected: httpReady,
    liveConnected,
    onChatMessage,
    sendRoomMessage,
    abortRun: messaging.abortRun,
    steerRun: (runId: string, text: string) => messaging.steerRun(runId, { text }),
    rpc,
  };
}
