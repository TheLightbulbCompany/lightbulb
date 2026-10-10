import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import type { CanonicalRoomMessage, LiveEvent, RoomDetail, RoomListResponse, RoomSection, RoomSummary, RunStatusResponse } from "@lightbulb/shared";

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
  topic: null,
  starred: false,
  muted: false,
  sectionId: null,
  lastReadSeq: 0,
  createdAt: "2026-09-10T00:00:00Z",
  pinnedMessageIds: [],
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
  listRooms: vi.fn<() => Promise<RoomListResponse>>(async () => ({ rooms: [room as RoomSummary], sections: [] })),
  getRoom: vi.fn<(roomId: string) => Promise<RoomDetail>>(async () => room),
  listMessages: vi.fn(async () => ({ messages: [firstMessage], nextAfterSeq: null as number | null })),
  sendMessage: vi.fn(async (_roomId: string, request: { clientMessageId: string; text: string; mentions: unknown[]; replyToMessageId: string | null; threadRootMessageId: string | null }) => ({
    ...firstMessage,
    messageId: "55555555-5555-4555-8555-555555555555",
    messageSeq: 2,
    text: request.text,
    mentions: request.mentions,
  })),
  markRead: vi.fn(async () => ({ roomId: room.roomId, lastReadSeq: 1 })),
  markUnread: vi.fn(async () => ({ roomId: room.roomId, lastReadSeq: 0 })),
  setTyping: vi.fn(async () => undefined),
  createRoom: vi.fn(), updateRoom: vi.fn(), upsertMember: vi.fn(), removeMember: vi.fn(), abortRun: vi.fn(), steerRun: vi.fn(), listSubagents: vi.fn().mockResolvedValue({ subagents: [] }), subagentTail: vi.fn(), steerSubagent: vi.fn(), stopSubagent: vi.fn(), getLiveConfig: vi.fn(),
  setMemberPrefs: vi.fn(), putSections: vi.fn(), leaveRoom: vi.fn(), setPin: vi.fn(),
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

import { mergeCanonicalMessages } from "@lightbulb/shared";
import { useRooms } from "../useRooms";

it("keeps newer reactions when message snapshots arrive out of order", () => {
  const latest = { ...firstMessage, reactionRevision: 2, reactions: { "👍": ["user-a"] } };
  const stale = { ...latest, reactionRevision: 1, reactions: {} };
  expect(mergeCanonicalMessages([latest], [stale])).toEqual([latest]);
  expect(mergeCanonicalMessages([stale], [latest])).toEqual([latest]);
});

it("never resurrects edited or deleted text from a newer reaction snapshot", () => {
  const edited = { ...firstMessage, text: "corrected", messageRevision: 1, editedAt: "now", reactionRevision: 1 };
  const reaction = { ...firstMessage, reactionRevision: 2, reactions: { "👍": ["user-a"] } };
  const combined = mergeCanonicalMessages([edited], [reaction])[0];
  expect(combined.text).toBe("corrected");
  expect(combined.reactions).toEqual(reaction.reactions);
  const deleted = { ...edited, text: "", messageRevision: 2, deletedAt: "now", reactions: {} };
  expect(mergeCanonicalMessages([deleted], [reaction])[0]).toMatchObject({ text: "", deletedAt: "now", reactions: {} });
  expect(mergeCanonicalMessages([reaction], [deleted])[0]).toMatchObject({ text: "", deletedAt: "now", reactions: {} });
});

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
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  handlers.clear();
  live.liveConnected = false;
  live.liveChannels = [];
  live.connectionRevision = 1;
  live.claimActiveRoomSubscription.mockClear();
  releaseActiveRoomSubscription.mockClear();
  for (const method of Object.values(messaging)) {
    if (typeof method === "function" && "mockClear" in method) (method as ReturnType<typeof vi.fn>).mockClear();
  }
  messaging.listRooms.mockResolvedValue({ rooms: [room], sections: [] });
  messaging.getRoom.mockResolvedValue(room);
  messaging.listMessages.mockResolvedValue({ messages: [firstMessage], nextAfterSeq: null });
});

describe("useRooms canonical reconciliation", () => {
  it("does not immediately re-mark a visible room read after marking it unread", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    act(() => { for (const handler of handlers) handler(messageEvent(firstMessage)); });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalled());
    messaging.markRead.mockClear();

    await act(async () => result.current.markUnread(room.roomId));
    act(() => window.dispatchEvent(new Event("focus")));

    expect(messaging.markUnread).toHaveBeenCalledWith(room.roomId, undefined);
    expect(messaging.markRead).not.toHaveBeenCalled();

    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalled());
  });

  it("waits for an in-flight automatic read before marking the room unread", async () => {
    const latestMessage = { ...firstMessage, messageSeq: 4 };
    let finishRead!: () => void;
    messaging.markRead.mockImplementationOnce(() => new Promise((resolve) => {
      finishRead = () => resolve({ roomId: room.roomId, lastReadSeq: 4 });
    }));
    messaging.markUnread.mockResolvedValueOnce({ roomId: room.roomId, lastReadSeq: 3 });
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    act(() => { for (const handler of handlers) handler(messageEvent(latestMessage)); });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalled());
    expect(messaging.markRead).toHaveBeenCalledWith(room.roomId, { lastReadSeq: 4 });

    let unread!: Promise<unknown>;
    act(() => { unread = result.current.markUnread(room.roomId); });
    expect(messaging.markUnread).not.toHaveBeenCalled();
    await act(async () => { finishRead(); await unread; });
    expect(messaging.markUnread).toHaveBeenCalledWith(room.roomId, undefined);
  });
  it("reports complete history only after all pages and exposes failed loads for retry", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    messaging.listMessages.mockResolvedValueOnce({ messages: [firstMessage], nextAfterSeq: 1 });
    messaging.listMessages.mockRejectedValueOnce(new Error("offline"));
    act(() => result.current.loadHistory());
    await waitFor(() => expect(result.current.historyError).toBe(true));
    expect(result.current.historyComplete).toBe(false);
    expect(result.current.messages).toEqual([firstMessage]);
    act(() => result.current.loadHistory());
    await waitFor(() => expect(result.current.historyComplete).toBe(true));
    expect(result.current.historyError).toBe(false);
    expect(result.current.historyLoading).toBe(false);
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 1, 100, { signal: expect.any(AbortSignal) });
  });
  it("reconciles edits from history and retains them against delayed creation events", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const edited = { ...firstMessage, text: "corrected", messageRevision: 1, editedAt: "now" };
    messaging.listMessages.mockResolvedValue({ messages: [edited], nextAfterSeq: null });
    act(() => { for (const handler of handlers) handler({ schemaVersion: 1, eventId: "edit", type: "room.message.updated", occurredAt: "now", roomId: room.roomId,
      payload: { roomId: room.roomId, messageId: firstMessage.messageId, messageSeq: 1 } }); });
    await waitFor(() => expect(result.current.messages[0]?.text).toBe("corrected"));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { signal: expect.any(AbortSignal) });
    act(() => { for (const handler of handlers) handler(messageEvent(firstMessage)); });
    expect(result.current.messages[0].text).toBe("corrected");
  });
  it("keeps background arrivals unread until the window gains focus", async () => {
    vi.mocked(document.hasFocus).mockReturnValue(false);
    const { result } = renderHook(() => useRooms(), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    act(() => { for (const handler of handlers) handler(messageEvent(firstMessage)); });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(messaging.markRead).not.toHaveBeenCalled();
    vi.mocked(document.hasFocus).mockReturnValue(true);
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalled());
  });

  it("marks a session's chat slot read on open and on arrival while focused, and never while unfocused", async () => {
    const { result } = renderHook(() => useRooms(null, true, true, false), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalledWith(room.roomId, { lastReadSeq: 1 }));
    messaging.markRead.mockClear();
    vi.mocked(document.hasFocus).mockReturnValue(false);
    act(() => { for (const handler of handlers) handler(messageEvent({ ...firstMessage, messageId: "message-2", messageSeq: 2 })); });
    await waitFor(() => expect(result.current.messages).toHaveLength(2));
    expect(messaging.markRead).not.toHaveBeenCalled(); // visible but unfocused: nobody saw it, and a toast still fires
    vi.mocked(document.hasFocus).mockReturnValue(true);
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalledWith(room.roomId, { lastReadSeq: 2 }));
  });

  it("keeps a covered room unread, and marks it read as soon as it is uncovered", async () => {
    const { result, rerender } = renderHook(({ roomVisible }) => useRooms(null, true, roomVisible), {
      wrapper, initialProps: { roomVisible: false },
    });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    act(() => { for (const handler of handlers) handler(messageEvent(firstMessage)); });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    act(() => window.dispatchEvent(new Event("focus")));
    expect(messaging.markRead).not.toHaveBeenCalled();
    // No focus event: closing the covering session is itself the visibility change.
    rerender({ roomVisible: true });
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalled());
  });

  it("keeps an inactive room unread and refreshes the directory on private hints", async () => {
    const { result } = renderHook(() => useRooms(null, false), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    act(() => { for (const handler of handlers) handler(messageEvent(firstMessage)); });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(messaging.markRead).not.toHaveBeenCalled();
    messaging.listRooms.mockClear();
    act(() => { for (const handler of handlers) handler({
      type: "room.message.available", schemaVersion: 1, eventId: "hint", occurredAt: firstMessage.createdAt,
      roomId: "other", payload: { roomId: "other", messageId: "new", messageSeq: 3, threadRootMessageId: null, notify: false },
    }); });
    await waitFor(() => expect(messaging.listRooms).toHaveBeenCalled());
  });
  it("keeps the triggering thread on live agent runs", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const runId = "thread-run";
    act(() => {
      for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "thread-frame", type: "room.run.frame", occurredAt: "2026-09-10T00:01:00Z",
        payload: { roomId: room.roomId, agentId: room.members[1].id, runId, threadRootMessageId: firstMessage.messageId,
          frame: { type: "chunk", content: "Thread answer", runId, agentId: room.members[1].id } },
      });
    });
    expect(result.current.runs[0]).toMatchObject({ runId, threadRootMessageId: firstMessage.messageId });
  });
  it("drops a completed run once its text is persisted under a different runId", async () => {
    // The persisted message's own runId can miss the live run's (e.g. a retry
    // reassigns it) -- fall back to matching the same agent's identical text
    // so the completed run card doesn't linger below the real message.
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const runId = "run-dup-1";
    const agentId = room.members[1].id;
    act(() => {
      for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "frame-chunk", type: "room.run.frame", occurredAt: "2026-09-10T00:01:00Z",
        payload: { roomId: room.roomId, agentId, runId, frame: { type: "chunk", content: "Persisted reply", runId, agentId } },
      });
    });
    act(() => {
      for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "frame-end", type: "room.run.frame", occurredAt: "2026-09-10T00:02:00Z",
        payload: { roomId: room.roomId, agentId, runId, frame: { type: "run_ended", outcome: "done", runId, agentId } },
      });
    });
    await waitFor(() => expect(result.current.runs).toEqual([expect.objectContaining({ runId, state: "completed", text: "Persisted reply" })]));

    messaging.listRooms.mockClear();
    const persisted: CanonicalRoomMessage = { ...firstMessage, messageId: "persisted-1", messageSeq: 2, runId: "different-run-id",
      author: { kind: "agent", id: agentId, displayName: "Friday" }, text: "Persisted reply" };
    act(() => { for (const handler of handlers) handler(messageEvent(persisted)); });
    await waitFor(() => expect(result.current.messages.some((message) => message.messageId === persisted.messageId)).toBe(true));
    // `room.message.created` unconditionally revalidates the room list (mutateRooms());
    // let that settle here, or its dangling promise fires after this test unmounts.
    await waitFor(() => expect(messaging.listRooms).toHaveBeenCalled());

    expect(result.current.runs).toEqual([]);
  });
  it("replaces active room state with the member upsert response", async () => {
    const updated = { ...room, roomRevision: 2, members: [...room.members, { ...room.members[1], id: "agent-atlas", displayName: "Atlas" }] };
    messaging.upsertMember.mockResolvedValue(updated);
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));

    await act(async () => result.current.upsertMember({ kind: "agent", id: "agent-atlas", role: "member", listenMode: "mentions" }));

    // Not a deep equality: opening a room now marks it read locally instead of
    // re-reading it, so `lastReadSeq`/`unreadCount` move on their own.
    expect(result.current.activeRoom).toMatchObject({
      roomId: updated.roomId, roomRevision: 2, members: updated.members,
    });
  });
  it("waits for the room list before selecting an existing agent DM", async () => {
    const fridayDm = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111116",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[1], id: "agent-friday" }],
    };
    let resolveRooms!: (value: RoomListResponse) => void;
    const pendingRooms = new Promise<RoomListResponse>((resolve) => { resolveRooms = resolve; });
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
      resolveRooms({ rooms: [room, fridayDm], sections: [] });
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
    messaging.listRooms.mockResolvedValue({ rooms: [fridayDm, atlasDm], sections: [] });
    messaging.getRoom.mockImplementation(async (roomId: string) =>
      roomId === atlasDm.roomId ? atlasDm : fridayDm,
    );

    const { result } = renderHook(() => useRooms("agent-friday"), { wrapper });
    await waitFor(() => expect(result.current.directoryRoomDetails.size).toBe(2));
    await act(async () => result.current.selectAgentDm("agent-atlas"));

    expect(result.current.activeRoomId).toBe(atlasDm.roomId);
    expect(messaging.createRoom).not.toHaveBeenCalled();
  });

  it("selects an existing human DM without creating a duplicate", async () => {
    const colleagueDm = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111122",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[0], id: "user-colleague", displayName: "Colleague", role: "member" as const }],
    };
    messaging.listRooms.mockResolvedValue({ rooms: [room, colleagueDm], sections: [] });
    messaging.getRoom.mockImplementation(async (roomId: string) => roomId === colleagueDm.roomId ? colleagueDm : room);
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.directoryRoomDetails.has(colleagueDm.roomId)).toBe(true));

    await act(async () => result.current.selectDm({ kind: "human", id: "user-colleague" }));

    expect(result.current.activeRoomId).toBe(colleagueDm.roomId);
    expect(messaging.createRoom).not.toHaveBeenCalled();
  });

  it("names every direct message from the room list alone, with no per-room fetch", async () => {
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
    messaging.listRooms.mockResolvedValue({ rooms: [room, fridayDm, atlasDm], sections: [] });

    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.directoryRoomDetails.size).toBe(3));

    expect(result.current.directoryRoomDetails.get(fridayDm.roomId)?.members).toEqual(fridayDm.members);
    expect(result.current.directoryRoomDetails.get(atlasDm.roomId)?.members).toEqual(atlasDm.members);
    expect(result.current.directoryError).toBeUndefined();
    expect(result.current.directoryLoading).toBe(false);
    // The point of the change: the sidebar IS the room list. Only the active
    // room is fetched on its own, for its delivery key.
    expect(messaging.getRoom).not.toHaveBeenCalledWith(fridayDm.roomId);
    expect(messaging.getRoom).not.toHaveBeenCalledWith(atlasDm.roomId);
  });

  it("does not create a DM when the room list cannot be read", async () => {
    // Ruling out an existing DM means reading the room list. If that fails we
    // cannot know whether one already exists, and creating another would leave
    // the owner with a duplicate conversation.
    messaging.listRooms.mockRejectedValue(new Error("Conversations are unavailable"));
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.roomsError).toBeInstanceOf(Error));

    await expect(result.current.selectAgentDm("agent-atlas")).rejects.toThrow();

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

  it("creates a human DM with the human listen-mode contract", async () => {
    const created = {
      ...room,
      roomId: "11111111-1111-4111-8111-111111111123",
      kind: "dm" as const,
      name: null,
      members: [room.members[0], { ...room.members[0], id: "user-new", displayName: "New person", role: "member" as const }],
    };
    messaging.createRoom.mockResolvedValue(created);
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoomId).toBe(room.roomId));

    const participant = { kind: "human" as const, id: "user-new", displayName: "New person", mentionHandle: null };
    await act(async () => result.current.selectDm(participant));

    expect(messaging.createRoom).toHaveBeenCalledWith(expect.objectContaining({
      kind: "dm",
      name: null,
      members: [{ kind: "human", id: "user-new", role: "member", listenMode: null }],
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
    // Resolving an agent to a DM awaits the room list when it is not yet
    // loaded. A selection made during that window must win over the answer.
    let resolveRooms!: (value: RoomListResponse) => void;
    messaging.listRooms.mockReturnValue(new Promise<RoomListResponse>((resolve) => { resolveRooms = resolve; }));

    const { result } = renderHook(() => useRooms(), { wrapper });
    const selectingFriday = result.current.selectAgentDm("agent-friday");
    act(() => result.current.selectRoom(room.roomId));

    await act(async () => {
      resolveRooms({ rooms: [room, fridayDm], sections: [] });
      await selectingFriday.catch(() => {});
    });

    expect(result.current.activeRoomId).toBe(room.roomId);
    expect(messaging.createRoom).not.toHaveBeenCalled();
  });

  it("does not let a rejected initial DM selection override a newer room selection", async () => {
    const otherChannel = { ...room, roomId: "11111111-1111-4111-8111-111111111121", name: "General" };
    let rejectCreate!: (error: Error) => void;
    messaging.listRooms.mockResolvedValue({ rooms: [room, otherChannel], sections: [] });
    messaging.createRoom.mockImplementation(() => new Promise<RoomDetail>((_, reject) => { rejectCreate = reject; }));
    const { result } = renderHook(() => useRooms("agent-new"), { wrapper });
    await waitFor(() => expect(messaging.createRoom).toHaveBeenCalledOnce());

    act(() => result.current.selectRoom(otherChannel.roomId));
    await act(async () => { rejectCreate(new Error("Creation failed")); });

    expect(result.current.activeRoomId).toBe(otherChannel.roomId);
  });

  it("loads history without waiting for the channel, merges gaps in sequence, rotates keys, and evicts access immediately", async () => {
    const roomWithWorkspace: typeof room = { ...room, workspaceId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" };
    messaging.getRoom.mockResolvedValue(roomWithWorkspace);

    const { result, rerender } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(live.claimActiveRoomSubscription).toHaveBeenCalledWith({ roomId: room.roomId, deliveryKey: room.deliveryKey, workspaceId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" });

    // History does NOT wait for the subscription to be acknowledged. That
    // channel only ever delivered future messages, and any gap is closed by the
    // afterSeq reconciliation exercised further down this test.
    await waitFor(() => expect(result.current.messages.map((message) => message.messageSeq)).toEqual([1]));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) });

    live.liveConnected = true;
    live.liveChannels = [`/users/private`, `/rooms/${room.roomId}/${room.deliveryKey}`];
    rerender();

    const third = { ...firstMessage, messageId: "66666666-6666-4666-8666-666666666666", messageSeq: 3, text: "Third" };
    const second = { ...firstMessage, messageId: "77777777-7777-4777-8777-777777777777", messageSeq: 2, text: "Second" };
    messaging.listMessages.mockResolvedValueOnce({ messages: [second, third], nextAfterSeq: null });
    act(() => {
      for (const handler of handlers) handler(messageEvent(third));
    });
    await waitFor(() => expect(result.current.messages.map((message) => message.messageSeq)).toEqual([1, 2, 3]));
    expect(messaging.listMessages).toHaveBeenLastCalledWith(room.roomId, 1, 100, { signal: expect.any(AbortSignal) });
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
    expect(live.claimActiveRoomSubscription).toHaveBeenLastCalledWith({ roomId: room.roomId, deliveryKey: rotated.payload.deliveryKey, workspaceId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" });
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
      workspaceId: null,
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

  it("does not merge a late send response into a newly selected room", async () => {
    const otherRoom = { ...room, roomId: "11111111-1111-4111-8111-111111111122", name: "General" };
    let resolveSend!: (message: CanonicalRoomMessage) => void;
    messaging.listRooms.mockResolvedValue({ rooms: [room, otherRoom], sections: [] });
    messaging.getRoom.mockImplementation(async (roomId: string) => roomId === otherRoom.roomId ? otherRoom : room);
    messaging.sendMessage.mockImplementationOnce(() => new Promise((resolve) => { resolveSend = resolve; }));
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const request = {
      clientMessageId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
      text: "Room A only",
      mentions: [],
      replyToMessageId: null,
      threadRootMessageId: null,
    };

    let sending!: Promise<CanonicalRoomMessage>;
    act(() => { sending = result.current.sendMessage(request); });
    act(() => result.current.selectRoom(otherRoom.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(otherRoom.roomId));
    await act(async () => {
      resolveSend({ ...firstMessage, messageId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", messageSeq: 2, text: request.text });
      await sending;
    });

    expect(result.current.messages).toEqual([]);
    expect(result.current.optimisticMessages).toEqual([]);
    expect(result.current.sendState(request.clientMessageId)).toBe("idle");
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

  it("stars a room optimistically and revalidates the list", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    // The server confirms the star, so the post-mutation revalidate (the
    // `finally { mutateRooms() }` reconciliation) also comes back starred --
    // this exercises both the optimistic flip and the settled truth.
    messaging.setMemberPrefs.mockResolvedValueOnce({ ...room, starred: true });
    messaging.listRooms.mockResolvedValueOnce({ rooms: [{ ...room, starred: true }], sections: [] });

    await act(async () => { await result.current.setRoomPrefs(room.roomId, { starred: true }); });

    expect(messaging.setMemberPrefs).toHaveBeenCalledWith(room.roomId, { starred: true });
    await waitFor(() => expect(result.current.rooms[0].starred).toBe(true));
  });

  // Regression for the row menu's "Move to <section>" item not appearing
  // until a full reload: saveSections used to write the server's response
  // into the cache with a wholesale replace and no trailing revalidate, so a
  // slower background revalidate (fired by any live event, undebounced) that
  // started before this save could resolve afterward and silently clobber
  // the section back out of the cache the row menu reads from.
  it("makes a newly saved section available through the cache immediately, before the trailing revalidate settles", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(result.current.sections).toEqual([]);

    const saved: RoomSection = { id: "s-1", name: "Ops" };
    messaging.putSections.mockResolvedValueOnce({ rooms: [room], sections: [saved] });
    let resolveRevalidate!: (value: RoomListResponse) => void;
    messaging.listRooms.mockImplementationOnce(() => new Promise((resolve) => { resolveRevalidate = resolve; }));

    let saving!: Promise<void>;
    act(() => { saving = result.current.saveSections([saved]); });
    expect(messaging.putSections).toHaveBeenCalledWith({ sections: [saved] });

    // The section is already in the cache from the response-driven write --
    // this is what a row's "Move to Ops" item reads -- well before the
    // trailing `mutateRooms()` revalidate below is allowed to resolve.
    await waitFor(() => expect(result.current.sections).toEqual([saved]));
    expect(result.current.rooms).toEqual([room]);

    await act(async () => { resolveRevalidate({ rooms: [room], sections: [saved] }); await saving; });
    expect(result.current.sections).toEqual([saved]);
  });

  it("passes fromSeq through to mark unread", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));

    await act(async () => { await result.current.markUnread(room.roomId, 3); });

    expect(messaging.markUnread).toHaveBeenCalledWith(room.roomId, 3);
  });

  it("revalidates the room list on a member-updated live event", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper });
    await waitFor(() => expect(result.current.rooms).toHaveLength(1));
    messaging.listRooms.mockClear();

    act(() => {
      // Published on the user channel (not a room subscription), so the
      // real wire frame carries no envelope-level roomId -- only payload.roomId.
      for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "member-updated", type: "room.member.updated", occurredAt: "2026-09-10T00:03:00Z",
        payload: { roomId: room.roomId },
      });
    });

    await waitFor(() => expect(messaging.listRooms).toHaveBeenCalled());
  });
});
