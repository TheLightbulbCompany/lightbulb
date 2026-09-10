import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import type { CanonicalRoomMessage, LiveEvent, RoomDetail, RoomSummary, RunStatusResponse } from "@lightbulb/shared";

const room: RoomDetail = {
  roomId: "11111111-1111-4111-8111-111111111111",
  name: "Launch",
  kind: "channel",
  roomRevision: 1,
  deliveryKey: "22222222-2222-4222-8222-222222222222",
  lastMessageSeq: 1,
  lastMessageAt: "2026-09-10T00:00:00Z",
  unreadCount: 1,
  updatedAt: "2026-09-10T00:00:00Z",
  lastReadSeq: 0,
  createdAt: "2026-09-10T00:00:00Z",
  members: [
    { kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 },
    { kind: "agent", id: "33333333-3333-4333-8333-333333333333", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "active", joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 },
  ],
};
const firstMessage: CanonicalRoomMessage = {
  messageId: "44444444-4444-4444-8444-444444444444",
  roomId: room.roomId,
  messageSeq: 1,
  roomRevision: 1,
  author: { kind: "human", id: "user-a", displayName: "Alice" },
  text: "First",
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-10T00:00:00Z",
  runId: null,
};

const handlers = new Set<(event: LiveEvent) => void>();
const messaging = {
  listRooms: vi.fn(async () => ({ rooms: [room as RoomSummary] })),
  getRoom: vi.fn(async () => room),
  listMessages: vi.fn(async () => ({ messages: [firstMessage], nextAfterSeq: null })),
  sendMessage: vi.fn(async (_roomId: string, request: { clientMessageId: string; text: string; mentions: unknown[]; replyToMessageId: string | null; threadRootMessageId: string | null }) => ({
    ...firstMessage,
    messageId: "55555555-5555-4555-8555-555555555555",
    messageSeq: 2,
    text: request.text,
    mentions: request.mentions,
  })),
  markRead: vi.fn(async () => ({ roomId: room.roomId, lastReadSeq: 1 })),
  setTyping: vi.fn(async () => undefined),
  createRoom: vi.fn(), updateRoom: vi.fn(), upsertMember: vi.fn(), removeMember: vi.fn(), abortRun: vi.fn(), steerRun: vi.fn(), getLiveConfig: vi.fn(),
  getRunStatus: vi.fn(async (): Promise<RunStatusResponse> => ({ runId: "run-1", state: "running" })),
};
const releaseActiveRoomSubscription = vi.fn();
const live = {
  httpReady: true,
  liveConnected: false,
  liveChannels: [] as string[],
  connectionRevision: 1,
  messaging,
  claimActiveRoomSubscription: vi.fn(() => releaseActiveRoomSubscription),
  onEvent: (handler: (event: LiveEvent) => void) => {
    handlers.add(handler);
    return () => handlers.delete(handler);
  },
};
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, userId: "user-a", orgId: "org-a" }) }));

import { useRooms } from "../useRooms";

function wrapper({ children }: { children: ReactNode }) {
  return <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
}

function messageEvent(message: CanonicalRoomMessage): LiveEvent {
  return {
    schemaVersion: 1,
    eventId: `event-${message.messageId}`,
    type: "room.message.created",
    occurredAt: message.createdAt,
    roomId: message.roomId,
    messageSeq: message.messageSeq,
    roomRevision: message.roomRevision,
    payload: message,
  };
}

beforeEach(() => {
  handlers.clear();
  live.liveConnected = false;
  live.liveChannels = [];
  live.connectionRevision = 1;
  live.claimActiveRoomSubscription.mockClear();
  releaseActiveRoomSubscription.mockClear();
  for (const method of Object.values(messaging)) {
    if (typeof method === "function" && "mockClear" in method) (method as ReturnType<typeof vi.fn>).mockClear();
  }
  messaging.listRooms.mockResolvedValue({ rooms: [room] });
  messaging.getRoom.mockResolvedValue(room);
  messaging.listMessages.mockResolvedValue({ messages: [firstMessage], nextAfterSeq: null });
});

describe("useRooms canonical reconciliation", () => {
  it("subscribes before history, merges gaps in sequence, rotates keys, and evicts access immediately", async () => {
    const { result, rerender } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(live.claimActiveRoomSubscription).toHaveBeenCalledWith({ roomId: room.roomId, deliveryKey: room.deliveryKey });
    expect(messaging.listMessages).not.toHaveBeenCalled();

    live.liveConnected = true;
    live.liveChannels = [`/users/private`, `/rooms/${room.roomId}/${room.deliveryKey}`];
    rerender();
    await waitFor(() => expect(result.current.messages.map((message) => message.messageSeq)).toEqual([1]));

    const third = { ...firstMessage, messageId: "66666666-6666-4666-8666-666666666666", messageSeq: 3, text: "Third" };
    const second = { ...firstMessage, messageId: "77777777-7777-4777-8777-777777777777", messageSeq: 2, text: "Second" };
    messaging.listMessages.mockResolvedValueOnce({ messages: [second, third], nextAfterSeq: null });
    act(() => {
      for (const handler of handlers) handler(messageEvent(third));
    });
    await waitFor(() => expect(result.current.messages.map((message) => message.messageSeq)).toEqual([1, 2, 3]));
    expect(messaging.listMessages).toHaveBeenLastCalledWith(room.roomId, 1, 100);
    act(() => {
      for (const handler of handlers) handler(messageEvent(third));
    });
    expect(result.current.messages).toHaveLength(3);

    const rotated: LiveEvent = {
      schemaVersion: 1, eventId: "rotate", type: "room.subscription.rotated", occurredAt: "2026-09-10T00:01:00Z",
      payload: { roomId: room.roomId, deliveryKey: "88888888-8888-4888-8888-888888888888", roomRevision: 2 },
    };
    act(() => {
      for (const handler of handlers) handler(rotated);
    });
    expect(live.claimActiveRoomSubscription).toHaveBeenLastCalledWith({ roomId: room.roomId, deliveryKey: rotated.payload.deliveryKey });
    releaseActiveRoomSubscription.mockClear();

    act(() => {
      for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "removed", type: "room.access.removed", occurredAt: "2026-09-10T00:02:00Z",
        payload: { roomId: room.roomId, roomRevision: 3 },
      });
    });
    expect(result.current.accessRemoved).toBe(true);
    expect(result.current.messages).toEqual([]);
    expect(releaseActiveRoomSubscription).toHaveBeenCalledOnce();
  });

  it("claims live delivery only while its room workspace is active", async () => {
    const { result, rerender } = renderHook(
      ({ subscribeToRoom }) => useRooms(undefined, subscribeToRoom),
      { wrapper, initialProps: { subscribeToRoom: false } },
    );
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(live.claimActiveRoomSubscription).not.toHaveBeenCalled();

    rerender({ subscribeToRoom: true });
    await waitFor(() => expect(live.claimActiveRoomSubscription).toHaveBeenCalledWith({
      roomId: room.roomId,
      deliveryKey: room.deliveryKey,
    }));

    rerender({ subscribeToRoom: false });
    expect(releaseActiveRoomSubscription).toHaveBeenCalledOnce();
  });

  it("removes the sending bubble when delivery becomes uncertain", async () => {
    messaging.sendMessage.mockRejectedValueOnce(new Error("network unavailable"));
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const request = {
      clientMessageId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      text: "Check deployment",
      mentions: [],
      replyToMessageId: null,
      threadRootMessageId: null,
    };

    await act(async () => {
      await expect(result.current.sendMessage(request)).rejects.toThrow("network unavailable");
    });

    expect(result.current.optimisticMessages).toEqual([]);
    expect(result.current.sendState(request.clientMessageId)).toBe("uncertain");
  });

  it("sends over HTTP while live updates reconnect and keeps the canonical mention UUID", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const request = {
      clientMessageId: "99999999-9999-4999-8999-999999999999",
      text: "@friday investigate",
      mentions: [{ kind: "agent" as const, id: room.members[1].id }],
      replyToMessageId: null,
      threadRootMessageId: null,
    };
    await act(async () => result.current.sendMessage(request));
    expect(live.liveConnected).toBe(false);
    expect(messaging.sendMessage).toHaveBeenCalledWith(room.roomId, request);
    expect(result.current.messages.at(-1)).toMatchObject({ text: request.text, mentions: request.mentions });
  });

  it("clears a run stuck on 'running' by reconciling against the server on reconnect", async () => {
    // `room.run.frame` (carrying the terminal `run_ended`) is ephemeral -- a
    // frame dropped across a reconnect leaves `runs` stuck at "running"
    // forever with no other signal. On the next reconnect, useRooms must
    // check any still-"running" run against GET /messaging/runs/{runId} and
    // clear it once the server reports it terminal.
    const { result, rerender } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));

    live.liveConnected = true;
    live.liveChannels = [`/rooms/${room.roomId}/${room.deliveryKey}`];
    rerender();
    await waitFor(() => expect(result.current.messages.map((message) => message.messageSeq)).toEqual([1]));

    const runId = "run-stuck-1";
    const agentId = room.members[1].id;
    act(() => {
      for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "frame-1", type: "room.run.frame", occurredAt: "2026-09-10T00:01:00Z",
        payload: { roomId: room.roomId, agentId, runId, frame: { type: "tool_start", tool: "search", runId, agentId } },
      });
    });
    await waitFor(() => expect(result.current.runs).toEqual([expect.objectContaining({ runId, state: "running" })]));

    // The abort actually landed server-side, but the terminal frame never
    // reached this client -- simulate the reconnect that should trigger
    // reconciliation.
    messaging.getRunStatus.mockResolvedValueOnce({ runId, state: "terminal" });
    live.connectionRevision = 2;
    rerender();

    await waitFor(() => expect(messaging.getRunStatus).toHaveBeenCalledWith(runId));
    await waitFor(() => expect(result.current.runs).toEqual([expect.objectContaining({ runId, state: "completed" })]));
  });
});
