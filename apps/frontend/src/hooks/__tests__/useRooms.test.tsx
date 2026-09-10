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
  getRoom: vi.fn<(roomId: string) => Promise<RoomDetail>>(async () => room),
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
  it("waits for the room list before selecting an existing agent DM", async () => {
    const fridayDm = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111116",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[1], id: "agent-friday" }],
    };
    let resolveRooms!: (value: { rooms: RoomSummary[] }) => void;
    const pendingRooms = new Promise<{ rooms: RoomSummary[] }>((resolve) => { resolveRooms = resolve; });
    messaging.listRooms.mockReturnValue(pendingRooms);
    messaging.getRoom.mockImplementation(async (id) => id === fridayDm.roomId ? fridayDm : room);
    messaging.createRoom.mockResolvedValue({ ...fridayDm, roomId: "unwanted-duplicate" });
    const { result } = renderHook(() => useRooms(), { wrapper });

    let selectingFriday!: Promise<string | null>;
    await act(async () => { selectingFriday = result.current.selectAgentDm("agent-friday"); });

    expect(result.current.roomsLoading).toBe(true);
    expect(result.current.activeRoomId).toBeNull();
    expect(messaging.getRoom).not.toHaveBeenCalled();
    expect(messaging.createRoom).not.toHaveBeenCalled();

    await act(async () => {
      resolveRooms({ rooms: [room, fridayDm] });
      await selectingFriday;
    });

    expect(result.current.activeRoomId).toBe(fridayDm.roomId);
    expect(messaging.createRoom).not.toHaveBeenCalled();
  });

  it("hydrates direct-message members and retargets an existing DM", async () => {
    const fridayDm = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111112",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[1], id: "agent-friday", displayName: "Friday" }],
    };
    const atlasDm = {
      ...fridayDm,
      roomId: "11111111-1111-4111-8111-111111111113",
      members: [room.members[0], { ...room.members[1], id: "agent-atlas", displayName: "Atlas" }],
    };
    messaging.listRooms.mockResolvedValue({ rooms: [fridayDm, atlasDm] });
    messaging.getRoom.mockImplementation(async (roomId: string) =>
      roomId === atlasDm.roomId ? atlasDm : fridayDm,
    );

    const { result } = renderHook(() => useRooms("agent-friday"), { wrapper });
    await waitFor(() => expect(result.current.directoryRoomDetails.size).toBe(2));
    await act(async () => result.current.selectAgentDm("agent-atlas"));

    expect(result.current.activeRoomId).toBe(atlasDm.roomId);
    expect(messaging.createRoom).not.toHaveBeenCalled();
  });

  it("keeps partial directory identities visible and retries failed hydration", async () => {
    const fridayDm = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111117",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[1], id: "agent-friday" }],
    };
    const atlasDm = {
      ...fridayDm,
      roomId: "11111111-1111-4111-8111-111111111118",
      members: [room.members[0], { ...room.members[1], id: "agent-atlas", displayName: "Atlas" }],
    };
    let atlasAttempts = 0;
    messaging.listRooms.mockResolvedValue({ rooms: [room, fridayDm, atlasDm] });
    messaging.getRoom.mockImplementation(async (roomId: string) => {
      if (roomId === room.roomId) return room;
      if (roomId === fridayDm.roomId) return fridayDm;
      atlasAttempts += 1;
      if (atlasAttempts === 1) throw new Error("Atlas is temporarily unavailable");
      return atlasDm;
    });

    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.directoryError).toBeInstanceOf(Error));
    expect(result.current.directoryLoading).toBe(false);
    expect(result.current.directoryRoomDetails.get(fridayDm.roomId)).toEqual(fridayDm);

    await act(async () => { await result.current.refreshRooms(); });

    await waitFor(() => expect(result.current.directoryError).toBeUndefined());
    expect(result.current.directoryRoomDetails.size).toBe(2);
    expect(atlasAttempts).toBeGreaterThanOrEqual(2);
  });

  it("does not create a DM when incomplete hydration cannot rule out an existing agent", async () => {
    const fridayDm = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111119",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[1], id: "agent-friday" }],
    };
    const atlasDm = {
      ...fridayDm,
      roomId: "11111111-1111-4111-8111-111111111120",
      members: [room.members[0], { ...room.members[1], id: "agent-atlas", displayName: "Atlas" }],
    };
    messaging.listRooms.mockResolvedValue({ rooms: [room, fridayDm, atlasDm] });
    messaging.getRoom.mockImplementation(async (roomId: string) => {
      if (roomId === room.roomId) return room;
      if (roomId === fridayDm.roomId) return fridayDm;
      throw new Error("Atlas is temporarily unavailable");
    });
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.directoryError).toBeInstanceOf(Error));

    await expect(result.current.selectAgentDm("agent-atlas")).rejects.toThrow("Atlas is temporarily unavailable");

    expect(messaging.createRoom).not.toHaveBeenCalled();
  });

  it("creates one canonical DM when the requested agent has none", async () => {
    const created = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111114",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[1], id: "agent-new", displayName: "New agent" }],
    };
    messaging.createRoom.mockResolvedValue(created);
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoomId).toBe(room.roomId));

    await act(async () => result.current.selectAgentDm("agent-new"));

    expect(messaging.createRoom).toHaveBeenCalledWith(expect.objectContaining({
      kind: "dm",
      name: null,
      members: [{ kind: "agent", id: "agent-new", role: "member", listenMode: "active" }],
    }));
    expect(result.current.activeRoomId).toBe(created.roomId);
  });

  it("does not let a stale DM lookup override a newer room selection", async () => {
    const fridayDm = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111115",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[1], id: "agent-friday", displayName: "Friday" }],
    };
    const resolveFridayDetails: Array<(detail: RoomDetail) => void> = [];
    messaging.listRooms.mockResolvedValue({ rooms: [room, fridayDm] });
    messaging.getRoom.mockImplementation(async (roomId: string) => {
      if (roomId === room.roomId) return room;
      return new Promise<RoomDetail>((resolve) => resolveFridayDetails.push(resolve));
    });

    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoomId).toBe(room.roomId));

    const selectingFriday = result.current.selectAgentDm("agent-friday");
    await waitFor(() => expect(resolveFridayDetails).toHaveLength(2));
    act(() => result.current.selectRoom(room.roomId));
    await act(async () => {
      for (const resolve of resolveFridayDetails) resolve(fridayDm);
      await selectingFriday;
    });

    expect(result.current.activeRoomId).toBe(room.roomId);
  });

  it("does not let a rejected initial DM selection override a newer room selection", async () => {
    const otherChannel = { ...room, roomId: "11111111-1111-4111-8111-111111111121", name: "General" };
    let rejectCreate!: (error: Error) => void;
    messaging.listRooms.mockResolvedValue({ rooms: [room, otherChannel] });
    messaging.createRoom.mockImplementation(() => new Promise<RoomDetail>((_, reject) => { rejectCreate = reject; }));
    const { result } = renderHook(() => useRooms("agent-new"), { wrapper });
    await waitFor(() => expect(messaging.createRoom).toHaveBeenCalledOnce());

    act(() => result.current.selectRoom(otherChannel.roomId));
    await act(async () => { rejectCreate(new Error("Creation failed")); });

    expect(result.current.activeRoomId).toBe(otherChannel.roomId);
  });

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
