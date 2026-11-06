// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import type { CanonicalRoomMessage, LiveEvent, MessagePage, RoomDetail, RoomSection, RunStatusResponse } from "../index";
import type { ApiCallOptions } from "../api/client";
import { ApiError } from "../api/error";
import { useRooms as useSharedRooms, type RoomsLiveSource, type RoomsPlatform, type UseRoomsOptions } from "./useRooms";

const useRooms = (platform: RoomsPlatform, options: UseRoomsOptions = {}) =>
  useSharedRooms(platform, { boundedHistory: true, ...options });

const room: RoomDetail = {
  roomId: "room-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "key-1",
  lastMessageSeq: 1, lastMessageAt: "2026-09-11T00:00:00Z", unreadCount: 1,
  updatedAt: "2026-09-11T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null,
  lastReadSeq: 0, createdAt: "2026-09-11T00:00:00Z", pinnedMessageIds: [],
  members: [{ kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-11T00:00:00Z", joinedSeq: 0 }],
};
const message: CanonicalRoomMessage = {
  messageId: "msg-1", roomId: room.roomId, messageSeq: 1, roomRevision: 1,
  author: { kind: "human", id: "user-b", displayName: "Bob" }, text: "hi",
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: "2026-09-11T00:00:00Z", runId: null,
};

const handlers = new Set<(event: LiveEvent) => void>();
const messaging = {
  listRooms: vi.fn(async () => ({ rooms: [room], sections: [] as RoomSection[] })),
  getRoom: vi.fn<(roomId: string) => Promise<RoomDetail>>(async () => room),
  listMessages: vi.fn<(roomId: string, afterSeq: number, limit?: number, options?: ApiCallOptions & { beforeSeq?: number }) => Promise<MessagePage>>(
    async () => ({ messages: [message], nextAfterSeq: null, nextBeforeSeq: null }),
  ),
  markRead: vi.fn<(roomId: string, request: { lastReadSeq: number }) => Promise<{ roomId: string; lastReadSeq: number }>>(
    async () => ({ roomId: room.roomId, lastReadSeq: 1 }),
  ),
  uploadAttachment: vi.fn(async () => ({ id: "att-1", fileName: "a.png", contentType: "image/png" as const, size: 1 })),
  getRunStatus: vi.fn<(runId: string) => Promise<RunStatusResponse>>(async () => ({ runId: "run-1", state: "terminal" })),
  markUnread: vi.fn(), sendMessage: vi.fn(), setTyping: vi.fn(), createRoom: vi.fn(),
  updateRoom: vi.fn(), upsertMember: vi.fn(), removeMember: vi.fn(), abortRun: vi.fn(),
  steerRun: vi.fn(), listSubagents: vi.fn().mockResolvedValue({ subagents: [] }), subagentTail: vi.fn(), steerSubagent: vi.fn(), stopSubagent: vi.fn(), updateMessage: vi.fn(), setReaction: vi.fn(), downloadAttachment: vi.fn(),
  getLiveConfig: vi.fn(),
  setMemberPrefs: vi.fn(), putSections: vi.fn(), leaveRoom: vi.fn(), deleteRoom: vi.fn(), setPin: vi.fn(),
};

let visible = true;
const visibilityListeners = new Set<() => void>();
// Every field below is hoisted so it keeps one identity across renders, the way
// both real providers do (`onVisibilityChange` is contractually stable, and a
// fresh `liveChannels` array per render re-fires the reconcile effect forever).
const live: RoomsLiveSource = {
  messaging: messaging as never,
  onEvent: (handler) => { handlers.add(handler); return () => handlers.delete(handler); },
  claimActiveRoomSubscription: vi.fn(() => vi.fn()),
  // History does not wait on this; it is here because the contract still
  // provides it, and because `liveConnected` drives reconnect catch-up.
  liveChannels: [`/rooms/${room.roomId}/${room.deliveryKey}`],
  connectionRevision: 1, liveConnected: false, httpReady: true,
};
const isVisible = () => visible;
const onVisibilityChange = (listener: () => void) => {
  visibilityListeners.add(listener);
  return () => visibilityListeners.delete(listener);
};
const uuid = () => "11111111-1111-4111-8111-111111111111";
const platform = (over: Partial<RoomsPlatform> = {}): RoomsPlatform => ({
  userId: "user-a",
  orgId: "org-a",
  live,
  isVisible,
  onVisibilityChange,
  uuid,
  ...over,
});
const wrapper = ({ children }: { children: ReactNode }) =>
  <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;

beforeEach(() => {
  visible = true;
  live.liveChannels = [`/rooms/${room.roomId}/${room.deliveryKey}`];
  handlers.clear();
  visibilityListeners.clear();
  for (const fn of Object.values(messaging)) fn.mockClear();
  messaging.listRooms.mockResolvedValue({ rooms: [room], sections: [] });
  messaging.getRoom.mockResolvedValue(room);
  messaging.listMessages.mockResolvedValue({ messages: [message], nextAfterSeq: null, nextBeforeSeq: null });
  messaging.markRead.mockResolvedValue({ roomId: room.roomId, lastReadSeq: 1 });
});

describe("useRooms platform seams", () => {
  it("keeps full history for callers without an older-page control", async () => {
    const { result } = renderHook(() => useSharedRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { signal: expect.any(AbortSignal) });
    expect(messaging.listMessages.mock.calls.some(([, , , options]) => options?.beforeSeq !== undefined)).toBe(false);
  });

  it("marks read only while the platform reports itself visible", async () => {
    visible = false;
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(messaging.markRead).not.toHaveBeenCalled();

    visible = true;
    act(() => { for (const listener of visibilityListeners) listener(); });
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalledWith(room.roomId, { lastReadSeq: 1 }));
  });

  it("reconciles history when the live channel carries the workspace prefix", async () => {
    live.liveChannels = [`/ws/ws-1/rooms/${room.roomId}/${room.deliveryKey}`];
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) });
  });

  it("catches the room on screen up from the user channel while another column holds the room channel", async () => {
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    const reply = { ...message, messageId: "msg-2", messageSeq: 2, author: { kind: "agent" as const, id: "lb", displayName: "Lightbulb" } };
    messaging.listMessages.mockResolvedValue({ messages: [reply], nextAfterSeq: null, nextBeforeSeq: null });
    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-catch-up", type: "room.message.available", occurredAt: "2026-09-15T00:00:01Z",
      roomId: room.roomId, messageSeq: 2,
      payload: { roomId: room.roomId, messageId: "msg-2", messageSeq: 2, notify: true, threadRootMessageId: null, mentionedUserIds: [] },
    } as LiveEvent); });
    await waitFor(() => expect(result.current.messages.map((m) => m.messageId)).toEqual(["msg-1", "msg-2"]));
    expect(messaging.listMessages).toHaveBeenLastCalledWith(room.roomId, 1, 100, { signal: expect.any(AbortSignal) });
  });

  it("unsubscribes from visibility on unmount", () => {
    const { unmount } = renderHook(() => useRooms(platform()), { wrapper });
    expect(visibilityListeners.size).toBe(1);
    unmount();
    expect(visibilityListeners.size).toBe(0);
  });

  it("does not select or fetch any room when autoSelect is off", async () => {
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    await waitFor(() => expect(result.current.rooms).toHaveLength(1));
    expect(result.current.activeRoomId).toBeNull();
    expect(messaging.listMessages).not.toHaveBeenCalled();
  });

  it("leaves the workspace feed out of the list (no row, no unread, no auto-pick) but opens it by id", async () => {
    const feed = { ...room, roomId: "feed-1", feed: true, unreadCount: 7, lastMessageAt: "2026-09-12T00:00:00Z" };
    messaging.listRooms.mockResolvedValue({ rooms: [feed, room], sections: [] });
    messaging.getRoom.mockImplementation(async (roomId) => (roomId === feed.roomId ? feed : room));
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.activeRoomId).toBe(room.roomId));
    expect(result.current.rooms.map((r) => r.roomId)).toEqual([room.roomId]);
    act(() => result.current.selectRoom(feed.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(feed.roomId));
  });

  it("hides and refuses a DM with no recipient while preserving valid DMs", async () => {
    const recipient = {
      kind: "human" as const, id: "user-b", displayName: "Bob", mentionHandle: null,
      role: "member" as const, listenMode: null, joinedAt: "2026-09-11T00:00:00Z", joinedSeq: 0,
    };
    const phantom = { ...room, roomId: "dm-phantom", name: null, kind: "dm" as const };
    const valid = { ...phantom, roomId: "dm-valid", members: [...room.members, recipient] };
    messaging.listRooms.mockResolvedValue({ rooms: [phantom, valid], sections: [] });
    messaging.getRoom.mockImplementation(async (roomId) => roomId === valid.roomId ? valid : phantom);

    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    await waitFor(() => expect(result.current.rooms.map((entry) => entry.roomId)).toEqual([valid.roomId]));

    act(() => result.current.selectRoom(phantom.roomId));
    expect(result.current.activeRoomId).toBeNull();
    expect(messaging.getRoom).not.toHaveBeenCalledWith(phantom.roomId);

    act(() => result.current.selectRoom(valid.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(valid.roomId));
  });

  it("opens a room the list does not carry (a hidden session chat) through its detail", async () => {
    messaging.listRooms.mockResolvedValue({ rooms: [], sections: [] });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(messaging.getRoom).toHaveBeenCalledWith(room.roomId);
    await waitFor(() => expect(result.current.messages.map((m) => m.messageId)).toEqual(["msg-1"]));
    expect(live.claimActiveRoomSubscription).toHaveBeenCalledWith(expect.objectContaining({ roomId: room.roomId }));
  });

  it("uploads a DOM Blob and the phone's byte-carrying part through the same call", async () => {
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const blob = new Blob(["x"], { type: "image/png" });
    const native = { name: "a.png", type: "image/png", bytes: async () => new Uint8Array([1]) };
    await act(async () => { await result.current.uploadAttachment(blob); await result.current.uploadAttachment(native); });
    expect(messaging.uploadAttachment).toHaveBeenNthCalledWith(1, room.roomId, blob);
    expect(messaging.uploadAttachment).toHaveBeenNthCalledWith(2, room.roomId, native);
  });

  it("mints a client room id through the platform uuid, not global crypto", async () => {
    messaging.listRooms.mockResolvedValue({ rooms: [], sections: [] });
    messaging.createRoom.mockResolvedValue({ ...room, roomId: "room-2", kind: "dm" });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    await waitFor(() => expect(result.current.roomsLoading).toBe(false));
    await act(async () => { await result.current.selectDm({ kind: "agent", id: "agent-1" }); });
    expect(messaging.createRoom).toHaveBeenCalledWith(expect.objectContaining({
      clientRoomId: "11111111-1111-4111-8111-111111111111", kind: "dm",
    }));
  });

  it("two concurrent opens of one DM share a clientRoomId, so the server makes exactly one room", async () => {
    messaging.listRooms.mockResolvedValue({ rooms: [], sections: [] });
    // The server is idempotent on clientRoomId (rooms.py create): same id, same room.
    const made = new Map<string, RoomDetail>();
    let release!: () => void;
    const gate = new Promise<void>((done) => { release = done; });
    messaging.createRoom.mockImplementation(async ({ clientRoomId }: { clientRoomId: string }) => {
      await gate;
      if (!made.has(clientRoomId)) made.set(clientRoomId, { ...room, roomId: `dm-${made.size + 1}`, kind: "dm" });
      return made.get(clientRoomId)!;
    });
    let n = 0;
    const fresh = () => `00000000-0000-4000-8000-00000000000${++n}`;
    const { result } = renderHook(() => useRooms(platform({ uuid: fresh }), { autoSelect: false }), { wrapper });
    await waitFor(() => expect(result.current.roomsLoading).toBe(false));
    let both!: Promise<(string | null)[]>;
    await act(async () => {
      both = Promise.all([
        result.current.selectDm({ kind: "agent", id: "agent-1" }, { activate: false }),
        result.current.selectDm({ kind: "agent", id: "agent-1" }, { activate: false }),
      ]);
      await waitFor(() => expect(messaging.createRoom).toHaveBeenCalledTimes(2));
      release();
      await both;
    });
    const ids = messaging.createRoom.mock.calls.map(([request]) => (request as { clientRoomId: string }).clientRoomId);
    expect(new Set(ids).size).toBe(1);
    expect(made.size).toBe(1);
    // The later request wins; the superseded one resolves null rather than activating a stale room.
    expect(await both).toEqual([null, "dm-1"]);
  });

  // `activate: false` is the picker seam: a sheet that resolves a room only to
  // navigate to it must not leave its own hook instance holding that room, or
  // the detail SWR (refreshInterval 60s) polls it for the rest of the session.
  it("creates a DM without activating it when activate is false", async () => {
    messaging.listRooms.mockResolvedValue({ rooms: [], sections: [] });
    messaging.createRoom.mockResolvedValue({ ...room, roomId: "room-2", kind: "dm" });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    await waitFor(() => expect(result.current.roomsLoading).toBe(false));
    const opened: (string | null)[] = [];
    await act(async () => {
      opened.push(await result.current.selectDm({ kind: "agent", id: "agent-1" }, { activate: false }));
    });
    expect(opened).toEqual(["room-2"]);
    expect(result.current.activeRoomId).toBeNull();
    expect(messaging.getRoom).not.toHaveBeenCalled();
    expect(messaging.listMessages).not.toHaveBeenCalled();
  });

  it("returns an existing DM without activating it when activate is false", async () => {
    const dm: RoomDetail = {
      ...room, roomId: "room-dm", kind: "dm", name: null,
      members: [
        room.members[0],
        { kind: "agent", id: "agent-1", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "active", joinedAt: "2026-09-11T00:00:00Z", joinedSeq: 0 },
      ],
    };
    messaging.listRooms.mockResolvedValue({ rooms: [dm], sections: [] });
    messaging.getRoom.mockResolvedValue(dm);
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    await waitFor(() => expect(result.current.rooms).toHaveLength(1));
    const opened: (string | null)[] = [];
    await act(async () => {
      opened.push(await result.current.selectDm({ kind: "agent", id: "agent-1" }, { activate: false }));
    });
    expect(opened).toEqual(["room-dm"]);
    expect(result.current.activeRoomId).toBeNull();
    expect(messaging.createRoom).not.toHaveBeenCalled();
    expect(messaging.listMessages).not.toHaveBeenCalled();
  });

  it("creates a room without activating it when activate is false", async () => {
    messaging.listRooms.mockResolvedValue({ rooms: [], sections: [] });
    messaging.createRoom.mockResolvedValue({ ...room, roomId: "room-3", kind: "group-dm" });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    await waitFor(() => expect(result.current.roomsLoading).toBe(false));
    await act(async () => {
      await result.current.createRoom(
        { clientRoomId: "c1", kind: "group-dm", name: null, members: [{ kind: "agent", id: "agent-1", role: "member", listenMode: "active" }] },
        { activate: false },
      );
    });
    expect(result.current.activeRoomId).toBeNull();
    expect(messaging.listMessages).not.toHaveBeenCalled();
  });

  it("surfaces a failed resolve-or-create instead of leaving the surface loading", async () => {
    messaging.listRooms.mockResolvedValue({ rooms: [], sections: [] });
    messaging.createRoom.mockRejectedValueOnce(new Error("Room service is unavailable"));
    const { result } = renderHook(
      () => useRooms(platform(), { initialAgentId: "agent-1", autoSelect: false }),
      { wrapper },
    );
    // The only caller of selectAgentDm here is an effect whose `.catch` used to
    // discard this, leaving activeRoomId null and no error anywhere.
    await waitFor(() => expect(result.current.selectRoomError).toBe("Room service is unavailable"));
    expect(result.current.activeRoomId).toBeNull();

    messaging.createRoom.mockResolvedValue({ ...room, roomId: "room-2", kind: "dm" });
    await act(async () => { await result.current.selectAgentDm("agent-1"); });
    expect(result.current.selectRoomError).toBeNull();
  });

  it("names the reason a history read failed, and clears it on the retry", async () => {
    messaging.listMessages
      .mockRejectedValueOnce(new Error("History is temporarily unavailable"))
      .mockResolvedValue({ messages: [message], nextAfterSeq: null, nextBeforeSeq: null });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.historyErrorMessage).toBe("History is temporarily unavailable"));
    expect(result.current.historyError).toBe(true);

    act(() => result.current.loadHistory());
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(result.current.historyErrorMessage).toBeNull();
    expect(result.current.historyError).toBe(false);
  });

  it("treats a null userId as signed out and fetches nothing", async () => {
    renderHook(() => useRooms(platform({ userId: null })), { wrapper });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(messaging.listRooms).not.toHaveBeenCalled();
  });

  // The SWR key is null while identity is unresolved, so SWR's own isLoading
  // is false in that window -- indistinguishable from "fetched, zero rooms"
  // without this. Both apps gate the room surface behind an auth redirect, so
  // userId/orgId being null here only ever means "Clerk hasn't resolved yet",
  // never "signed out with no rooms".
  it("reports loading while identity is unresolved, then flips false once rooms resolve", async () => {
    const { result, rerender } = renderHook(
      ({ userId }: { userId: string | null }) => useRooms(platform({ userId })),
      { wrapper, initialProps: { userId: null as string | null } },
    );
    expect(result.current.roomsLoading).toBe(true);
    expect(messaging.listRooms).not.toHaveBeenCalled();

    rerender({ userId: "user-a" });
    await waitFor(() => expect(result.current.roomsLoading).toBe(false));
    expect(result.current.rooms).toHaveLength(1);
  });

  it("re-reads instead of claiming zero unread when the server returns a lower cursor", async () => {
    // `set_read_cursor` clamps to the room's own `lastSeq`, and another device
    // marking the room unread moves the cursor backwards — so the value that
    // comes back can be LOWER than the one sent. Applying it in place while
    // also declaring `unreadCount: 0` would state the room is caught up when it
    // demonstrably is not. The ordinary case skips a room GET and a room-list
    // GET; this case gives that up and re-reads, because the server is the
    // source of truth and guessing here is visible as an unread that never
    // clears or never appears.
    messaging.markRead.mockResolvedValueOnce({ roomId: room.roomId, lastReadSeq: 0 });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(messaging.markRead).toHaveBeenCalled());

    await waitFor(() => expect(messaging.getRoom).toHaveBeenCalled());
    expect(result.current.activeRoom?.unreadCount).not.toBe(0);
  });

  it("paints a revisited room from cache and asks only for the delta", async () => {
    const other: RoomDetail = { ...room, roomId: "room-2", name: "Second", deliveryKey: "key-2" };
    const otherMessage: CanonicalRoomMessage = { ...message, messageId: "msg-2", roomId: other.roomId, text: "elsewhere" };
    messaging.listRooms.mockResolvedValue({ rooms: [room, other], sections: [] });
    messaging.getRoom.mockImplementation(async (id: string) => (id === other.roomId ? other : room));
    messaging.listMessages.mockImplementation(async (roomId: string) => ({
      messages: roomId === other.roomId ? [otherMessage] : [message],
      nextAfterSeq: null,
    }));

    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) });

    await act(async () => { result.current.selectRoom(other.roomId); });
    await waitFor(() => expect(result.current.messages.map((m) => m.messageId)).toEqual(["msg-2"]));

    messaging.listMessages.mockClear();
    await act(async () => { result.current.selectRoom(room.roomId); });

    // Back where we started: the cached page is what renders, so there is no
    // window in which the timeline is empty, and the refetch is a delta rather
    // than a replay of the room from sequence zero.
    expect(result.current.messages.map((m) => m.messageId)).toEqual(["msg-1"]);
    await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 1, 100, { signal: expect.any(AbortSignal) }));
    expect(messaging.listMessages).not.toHaveBeenCalledWith(room.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) });
  });

  it("forgets cached history when the signed-in scope changes", async () => {
    const { result, rerender } = renderHook(
      ({ userId }: { userId: string }) => useRooms(platform({ userId })),
      { wrapper, initialProps: { userId: "user-a" } },
    );
    await waitFor(() => expect(result.current.messages).toHaveLength(1));

    messaging.listMessages.mockClear();
    rerender({ userId: "user-b" });

    // A different person must never be shown the previous one's messages, even
    // for the frame before their own history arrives.
    expect(result.current.messages).toHaveLength(0);
    await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) }));
  });

  describe("history cache shared across mounts", () => {
    // Mobile remounts the room screen on every push, so a per-instance cache
    // never survived a back-and-forth. One SWR cache (the app's) = one store.
    const oneCache = () => {
      const cache = new Map();
      return ({ children }: { children: ReactNode }) =>
        <SWRConfig value={{ provider: () => cache, dedupingInterval: 0 }}>{children}</SWRConfig>;
    };
    const cold = [room.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) }];
    const delta = [room.roomId, 1, 100, { signal: expect.any(AbortSignal) }];

    it("paints a remounted room from cache and asks only for the delta", async () => {
      const shared = oneCache();
      const first = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(first.result.current.messages).toHaveLength(1));
      first.unmount();

      // Held open: whatever renders now came from the cache, not the network.
      messaging.listMessages.mockClear();
      messaging.listMessages.mockImplementation(() => new Promise(() => {}));
      const second = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(second.result.current.activeRoomId).toBe(room.roomId));
      expect(second.result.current.messages.map((m) => m.messageId)).toEqual(["msg-1"]);
      await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith(...delta));
      expect(messaging.listMessages).not.toHaveBeenCalledWith(...cold);
    });

    it.each([
      ["another user", { userId: "user-b" }],
      ["another workspace", { orgId: "org-b" }],
    ])("never shows %s the cached history, and drops it on the switch", async (_label, other) => {
      const shared = oneCache();
      const first = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(first.result.current.messages).toHaveLength(1));
      first.unmount();

      messaging.listMessages.mockClear();
      messaging.listMessages.mockImplementation(() => new Promise(() => {}));
      const stranger = renderHook(() => useRooms(platform(other)), { wrapper: shared });
      await waitFor(() => expect(stranger.result.current.activeRoomId).toBe(room.roomId));
      expect(stranger.result.current.messages).toHaveLength(0);
      await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith(...cold));
      stranger.unmount();

      // Back to the first scope: its cache went with the switch.
      messaging.listMessages.mockClear();
      const again = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(again.result.current.activeRoomId).toBe(room.roomId));
      expect(again.result.current.messages).toHaveLength(0);
      await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith(...cold));
    });

    const hold = () => messaging.listMessages.mockImplementation(() => new Promise(() => {}));
    /** Load room-1 into `shared`'s store, then leave. */
    const warm = async (shared: ReturnType<typeof oneCache>) => {
      const first = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(first.result.current.messages).toHaveLength(1));
      first.unmount();
    };
    /** Open `roomId` on a fresh mount with every history read held open. */
    const reopen = async (shared: ReturnType<typeof oneCache>, roomId = room.roomId) => {
      messaging.listMessages.mockClear();
      hold();
      const next = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper: shared });
      await waitFor(() => expect(next.result.current.rooms.length).toBeGreaterThan(0));
      act(() => next.result.current.selectRoom(roomId));
      await waitFor(() => expect(next.result.current.activeRoomId).toBe(roomId));
      return next;
    };

    it("forgets a room on room.access.removed even when it is not the open one", async () => {
      const shared = oneCache();
      await warm(shared);
      const bystander = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper: shared });
      await waitFor(() => expect(bystander.result.current.rooms).toHaveLength(1));
      act(() => { for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "evt-removed", type: "room.access.removed", occurredAt: "2026-09-15T00:00:01Z",
        roomId: room.roomId, payload: { roomId: room.roomId, roomRevision: 2 },
      } as LiveEvent); });
      bystander.unmount();
      const again = await reopen(shared);
      expect(again.result.current.messages).toHaveLength(0);
    });

    it("forgets a room whose detail read answers 403", async () => {
      const shared = oneCache();
      await warm(shared);
      messaging.getRoom.mockRejectedValueOnce(new ApiError(403, null));
      const denied = await reopen(shared);
      await waitFor(() => expect(denied.result.current.accessRemoved).toBe(true));
      denied.unmount();
      const again = await reopen(shared);
      expect(again.result.current.messages).toHaveLength(0);
    });

    it("forgets a room a fresh room list no longer carries", async () => {
      const shared = oneCache();
      await warm(shared);
      messaging.listRooms.mockClear();
      messaging.listRooms.mockResolvedValueOnce({ rooms: [], sections: [] });
      const gone = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper: shared });
      await waitFor(() => expect(gone.result.current.rooms).toHaveLength(0));
      gone.unmount();
      const again = await reopen(shared);
      expect(again.result.current.messages).toHaveLength(0);
    });

    it("paints cached history only for a room the current list carries", async () => {
      const cache = new Map();
      const shared = ({ children }: { children: ReactNode }) =>
        <SWRConfig value={{ provider: () => cache, dedupingInterval: 0 }}>{children}</SWRConfig>;
      await warm(shared);
      // No list yet this time (cold, and held): nothing vouches for the room.
      for (const key of [...cache.keys()]) if (String(key).includes("messaging-rooms")) cache.delete(key);
      messaging.listRooms.mockImplementation(() => new Promise(() => {}));
      hold();
      const next = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper: shared });
      act(() => next.result.current.selectRoom(room.roomId));
      await waitFor(() => expect(next.result.current.activeRoomId).toBe(room.roomId));
      expect(next.result.current.messages).toHaveLength(0);
    });

    it("reopens a room from its last contiguous sequence, not past a gap", async () => {
      const shared = oneCache();
      const first = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(first.result.current.messages).toHaveLength(1));
      hold(); // the gap fill for seq 2 never lands
      act(() => { for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "evt-gap", type: "room.message.created", occurredAt: "2026-09-15T00:00:01Z",
        roomId: room.roomId, messageSeq: 3, roomRevision: 1, payload: { ...message, messageId: "msg-3", messageSeq: 3 },
      } as LiveEvent); });
      await waitFor(() => expect(first.result.current.messages).toHaveLength(2));
      first.unmount();
      await reopen(shared);
      await waitFor(() => expect(messaging.listMessages).toHaveBeenCalled());
      expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 1, 100, { signal: expect.any(AbortSignal) });
      expect(messaging.listMessages).not.toHaveBeenCalledWith(room.roomId, 3, 100, expect.anything());
    });

    it("merges two copies of one room instead of letting the staler one overwrite", async () => {
      const shared = oneCache();
      const second: CanonicalRoomMessage = { ...message, messageId: "msg-2", messageSeq: 2 };
      let staleB: (page: MessagePage) => void = () => {};
      // Echo the cursor: the fixed `1` would have the copy holding seq 2
      // re-mark forever (a lower server cursor triggers a re-read).
      messaging.markRead.mockImplementation(async (roomId, { lastReadSeq }) => ({ roomId, lastReadSeq }));
      messaging.listMessages
        .mockImplementationOnce(() => new Promise((resolve) => { staleB = resolve; }))
        .mockImplementationOnce(async () => ({ messages: [message, second], nextAfterSeq: null, nextBeforeSeq: null }))
        .mockImplementation(() => new Promise(() => {}));
      const b = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledTimes(1));
      const a = renderHook(() => useRooms(platform()), { wrapper: shared });
      await waitFor(() => expect(a.result.current.messages).toHaveLength(2));
      await act(async () => staleB({ messages: [message], nextAfterSeq: null, nextBeforeSeq: null }));
      await waitFor(() => expect(b.result.current.messages).toHaveLength(1));
      a.unmount();
      b.unmount();
      const again = await reopen(shared);
      expect(again.result.current.messages.map((m) => m.messageId)).toEqual(["msg-1", "msg-2"]);
    });

    it("keeps at most 20 rooms, evicting the least recently written", async () => {
      const shared = oneCache();
      const many = Array.from({ length: 21 }, (_, i) => ({ ...room, roomId: `room-${i}`, deliveryKey: `key-${i}` }));
      messaging.listRooms.mockResolvedValue({ rooms: many, sections: [] });
      messaging.getRoom.mockImplementation(async (id: string) => many.find((r) => r.roomId === id)!);
      messaging.listMessages.mockImplementation(async (roomId: string) => ({
        messages: [{ ...message, roomId, messageId: `msg-${roomId}` }], nextAfterSeq: null, nextBeforeSeq: null,
      }));
      const visitor = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper: shared });
      await waitFor(() => expect(visitor.result.current.rooms).toHaveLength(21));
      for (const r of many) {
        act(() => visitor.result.current.selectRoom(r.roomId));
        await waitFor(() => expect(visitor.result.current.messages[0]?.roomId).toBe(r.roomId));
      }
      visitor.unmount();
      expect((await reopen(shared, "room-0")).result.current.messages).toHaveLength(0);
      expect((await reopen(shared, "room-20")).result.current.messages).toHaveLength(1);
    });

    it("drops cached history on sign-out", async () => {
      const shared = oneCache();
      const { result, rerender } = renderHook(
        ({ userId }: { userId: string | null }) => useRooms(platform({ userId })),
        { wrapper: shared, initialProps: { userId: "user-a" as string | null } },
      );
      await waitFor(() => expect(result.current.messages).toHaveLength(1));
      rerender({ userId: null });
      expect(result.current.messages).toHaveLength(0);

      messaging.listMessages.mockClear();
      rerender({ userId: "user-a" });
      await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith(...cold));
    });
  });

  describe("several mounted copies share one room list", () => {
    // Mobile keeps three copies mounted (tab badge, Home, DMs); each used to
    // run its own coalescing timer and reconnect refetch.
    const three = () => [
      useRooms(platform(), { autoSelect: false }),
      useRooms(platform(), { autoSelect: false }),
      useRooms(platform(), { autoSelect: false }),
    ];

    it("refetches the list once per event burst", async () => {
      const { result } = renderHook(three, { wrapper });
      await waitFor(() => expect(result.current[2].rooms).toHaveLength(1));
      vi.useFakeTimers();
      try {
        messaging.listRooms.mockClear();
        act(() => {
          for (const handler of handlers) handler({
            schemaVersion: 1, eventId: "evt-member", type: "room.member.updated",
            occurredAt: "2026-09-15T00:00:01Z", roomId: room.roomId, roomRevision: 1, payload: {} as never,
          } as LiveEvent);
        });
        await act(async () => { await vi.advanceTimersByTimeAsync(500); });
        expect(messaging.listRooms).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("refetches the list once per reconnect", async () => {
      const { result, rerender } = renderHook(three, { wrapper });
      await waitFor(() => expect(result.current[2].rooms).toHaveLength(1));
      messaging.listRooms.mockClear();
      live.liveConnected = true;
      live.connectionRevision = 2;
      try {
        rerender();
        await waitFor(() => expect(messaging.listRooms).toHaveBeenCalled());
        await new Promise((resolve) => setTimeout(resolve, 20));
        expect(messaging.listRooms).toHaveBeenCalledTimes(1);
      } finally {
        live.liveConnected = false;
        live.connectionRevision = 1;
      }
    });

    it("keeps a list in flight when the live ack lands, and refetches after it (W14-4)", async () => {
      let first!: (value: { rooms: RoomDetail[]; sections: RoomSection[] }) => void;
      messaging.listRooms.mockImplementationOnce(() => new Promise((resolve) => { first = resolve; }));
      const { result, rerender } = renderHook(() => useRooms(platform()), { wrapper });
      await waitFor(() => expect(messaging.listRooms).toHaveBeenCalledTimes(1));
      live.liveConnected = true;
      live.connectionRevision = 2;
      try {
        rerender();
        await new Promise((resolve) => setTimeout(resolve, 20));
        expect(messaging.listRooms).toHaveBeenCalledTimes(1);
        await act(async () => first({ rooms: [room], sections: [] }));
        expect(result.current.roomsLoading).toBe(false);
        await waitFor(() => expect(messaging.listRooms).toHaveBeenCalledTimes(2));
      } finally {
        live.liveConnected = false;
        live.connectionRevision = 1;
      }
    });
  });

  describe("optimistic send reconciled by a live event", () => {
    // Prod incident: the server stored + returned 201, the canonical copy
    // arrived over the live channel and rendered, but the browser's own fetch
    // for the POST never settled -- so the optimistic row stayed on
    // "Sending..." forever, duplicating the canonical message.
    const sendRequest = {
      clientMessageId: "client-1", text: "shipping now",
      mentions: [] as never[], replyToMessageId: null, threadRootMessageId: null,
    };
    const confirmedEvent = (overrides: Partial<CanonicalRoomMessage> = {}): LiveEvent => ({
      schemaVersion: 1, eventId: "evt-confirmed", type: "room.message.created",
      occurredAt: "2026-09-15T00:00:01Z", roomId: room.roomId, messageSeq: 2, roomRevision: 1,
      payload: {
        ...message, messageId: "msg-confirmed", messageSeq: 2,
        author: { kind: "human", id: "user-a", displayName: "Alice" },
        text: sendRequest.text, replyToMessageId: null, threadRootMessageId: null,
        ...overrides,
      },
    });

    it("clears the optimistic row and marks the attempt persisted while the POST is still pending", async () => {
      messaging.sendMessage.mockImplementation(() => new Promise(() => {})); // never settles
      const { result } = renderHook(() => useRooms(platform()), { wrapper });
      act(() => result.current.selectRoom(room.roomId));
      await waitFor(() => expect(result.current.messages).toHaveLength(1));

      act(() => { void result.current.sendMessage(sendRequest); });
      await waitFor(() => expect(result.current.sendState("client-1")).toBe("sending"));
      expect(result.current.optimisticMessages.some((m) => m.clientMessageId === "client-1")).toBe(true);

      act(() => { for (const handler of handlers) handler(confirmedEvent()); });

      expect(result.current.optimisticMessages.some((m) => m.clientMessageId === "client-1")).toBe(false);
      expect(result.current.sendState("client-1")).toBe("persisted");
    });

    it("does not downgrade an already-persisted attempt when the hung POST later rejects", async () => {
      let rejectSend: ((reason: unknown) => void) | undefined;
      messaging.sendMessage.mockImplementation(
        () => new Promise((_resolve, reject) => { rejectSend = reject; }),
      );
      const { result } = renderHook(() => useRooms(platform()), { wrapper });
      act(() => result.current.selectRoom(room.roomId));
      await waitFor(() => expect(result.current.messages).toHaveLength(1));

      let sendPromise: Promise<unknown> = Promise.resolve();
      act(() => { sendPromise = result.current.sendMessage(sendRequest).catch(() => {}); });
      await waitFor(() => expect(result.current.sendState("client-1")).toBe("sending"));

      act(() => { for (const handler of handlers) handler(confirmedEvent()); });
      expect(result.current.sendState("client-1")).toBe("persisted");

      await act(async () => {
        rejectSend?.(new DOMException("The operation was aborted.", "AbortError"));
        await sendPromise;
      });
      expect(result.current.sendState("client-1")).toBe("persisted");
    });

    it("classifies a send whose POST aborts (times out) as uncertain, not rejected", async () => {
      const abortError = new DOMException("The operation was aborted.", "AbortError");
      messaging.sendMessage.mockRejectedValue(abortError);
      const { result } = renderHook(() => useRooms(platform()), { wrapper });
      act(() => result.current.selectRoom(room.roomId));
      await waitFor(() => expect(result.current.messages).toHaveLength(1));

      await act(async () => {
        await expect(result.current.sendMessage(sendRequest)).rejects.toBe(abortError);
      });

      expect(result.current.sendState("client-1")).toBe("uncertain");
      expect(result.current.optimisticMessages.some((m) => m.clientMessageId === "client-1")).toBe(false);
    });
  });

  describe("room list jumps on a live message hint", () => {
    // A new message must move its room to the top of the list (clients sort by
    // lastMessageAt) the moment the hint arrives — not after the coalesced
    // listRooms refetch lands ~400ms + a round trip later.
    const older: RoomDetail = {
      ...room, roomId: "room-old", name: "Old",
      lastMessageSeq: 1, lastMessageAt: "2026-09-11T00:00:00Z", unreadCount: 0, lastReadSeq: 1,
    };
    const newer: RoomDetail = {
      ...room, roomId: "room-new", name: "New",
      lastMessageSeq: 4, lastMessageAt: "2026-09-14T00:00:00Z", unreadCount: 0, lastReadSeq: 4,
    };
    const authored = (authorId: string, seq: number) => ({
      messageId: "msg-new", seq, authorId, authorKind: "human" as const, authorName: authorId,
      textPreview: "hi", hasAttachments: false, createdAt: "2026-09-15T00:00:01Z", threadRootMessageId: null,
    });
    const available = (
      messageSeq = 2, notify = true,
      extra: { authorId?: string; accountableHumanId?: string; threadRootMessageId?: string | null; mentionedUserIds?: string[] } = {},
    ): LiveEvent => ({
      schemaVersion: 1, eventId: "evt-avail", type: "room.message.available",
      occurredAt: "2026-09-15T00:00:01Z", roomId: older.roomId, messageSeq,
      payload: {
        roomId: older.roomId, messageId: "msg-new", messageSeq, notify,
        threadRootMessageId: extra.threadRootMessageId ?? null,
        lastMessage: authored(extra.authorId ?? "user-b", messageSeq),
        mentionedUserIds: extra.mentionedUserIds ?? [],
        ...(extra.accountableHumanId ? { accountableHumanId: extra.accountableHumanId } : {}),
      },
    } as LiveEvent);

    beforeEach(() => {
      messaging.listRooms.mockResolvedValue({ rooms: [newer, older], sections: [] });
    });

    it("bumps recency and unread synchronously, ahead of the coalesced refetch", async () => {
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));
      messaging.listRooms.mockClear();

      act(() => { for (const handler of handlers) handler(available()); });

      const bumped = result.current.rooms.find((r) => r.roomId === older.roomId);
      expect(bumped).toMatchObject({
        lastMessageSeq: 2, lastMessageAt: "2026-09-15T00:00:01Z", unreadCount: 1,
      });
      // The coalesced list refetch hasn't fired: this render came from the
      // optimistic patch, not the server.
      expect(messaging.listRooms).not.toHaveBeenCalled();
    });

    it("keeps an archived room archived when a late hint bumps it", async () => {
      // An archived room refuses posts, but a racing write can still emit a hint; the
      // sidebar sections on archivedAt, so the bump must carry it through untouched.
      const archivedAt = "2026-09-14T12:00:00Z";
      messaging.listRooms.mockResolvedValue({ rooms: [newer, { ...older, archivedAt }], sections: [] });
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => { for (const handler of handlers) handler(available()); });

      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ lastMessageSeq: 2, archivedAt });
    });

    it("drops a room whose access was removed even when it isn't the open one (a channel delete)", async () => {
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));
      messaging.listRooms.mockResolvedValue({ rooms: [newer], sections: [] });

      act(() => { for (const handler of handlers) handler({
        schemaVersion: 1, eventId: "evt-gone", type: "room.access.removed", occurredAt: "2026-09-15T00:00:02Z",
        roomId: older.roomId, payload: { roomId: older.roomId, roomRevision: 9 },
      } as LiveEvent); });

      await waitFor(() => expect(result.current.rooms.map((r) => r.roomId)).toEqual([newer.roomId]));
    });

    it("leaving the open room never reopens it from the stale list", async () => {
      messaging.leaveRoom.mockResolvedValue(undefined);
      messaging.listRooms.mockResolvedValue({ rooms: [newer, older], sections: [] });
      const { result } = renderHook(() => useRooms(platform()), { wrapper });
      await waitFor(() => expect(result.current.activeRoomId).toBe(newer.roomId));
      // The server list has not caught up yet: the refetch is still in flight.
      messaging.listRooms.mockReturnValue(new Promise(() => {}));

      await act(async () => { void result.current.leaveRoom(newer.roomId); });

      await waitFor(() => expect(result.current.activeRoomId).toBe(older.roomId));
      expect(result.current.rooms.map((r) => r.roomId)).toEqual([older.roomId]);
    });

    it("deleteRoom calls the command and refetches the list", async () => {
      messaging.deleteRoom.mockResolvedValue(undefined);
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));
      messaging.listRooms.mockResolvedValue({ rooms: [newer], sections: [] });

      await act(async () => { await result.current.deleteRoom(older.roomId); });

      expect(messaging.deleteRoom).toHaveBeenCalledWith(older.roomId);
      expect(result.current.rooms.map((r) => r.roomId)).toEqual([newer.roomId]);
    });

    it("counts a plain channel message as unread even when it does not notify", async () => {
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => { for (const handler of handlers) handler(available(2, false)); });

      const bumped = result.current.rooms.find((r) => r.roomId === older.roomId);
      expect(bumped).toMatchObject({
        lastMessageSeq: 2, lastMessageAt: "2026-09-15T00:00:01Z", unreadCount: 1, mentionCount: 0,
      });
    });

    it("never counts my own top-level post, which marks the room read like the server does", async () => {
      messaging.listRooms.mockResolvedValue({
        rooms: [newer, { ...older, unreadCount: 3, mentionCount: 1 }], sections: [],
      });
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => { for (const handler of handlers) handler(available(2, false, { authorId: "user-a" })); });

      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ lastMessageSeq: 2, lastReadSeq: 2, unreadCount: 0, mentionCount: 0 });
    });

    it("never counts a delegated top-level post owned by the current human", async () => {
      messaging.listRooms.mockResolvedValue({
        rooms: [newer, { ...older, unreadCount: 3, mentionCount: 1 }], sections: [],
      });
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => { for (const handler of handlers) handler(available(2, false, {
        authorId: "native-session", accountableHumanId: "user-a",
      })); });

      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ lastMessageSeq: 2, lastReadSeq: 2, unreadCount: 0, mentionCount: 0 });
    });

    it("counts a mention in a thread reply but never the reply as unread", async () => {
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => { for (const handler of handlers) handler(available(2, true, {
        threadRootMessageId: "msg-root", mentionedUserIds: ["user-a"],
      })); });

      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ lastMessageSeq: 2, unreadCount: 0, mentionCount: 1 });
    });

    it("caps both counts at 99", async () => {
      messaging.listRooms.mockResolvedValue({
        rooms: [newer, { ...older, unreadCount: 99, mentionCount: 99 }], sections: [],
      });
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => { for (const handler of handlers) handler(available(2, true, { mentionedUserIds: ["user-a"] })); });

      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ unreadCount: 99, mentionCount: 99 });
    });

    it("does not count a message at or below my read cursor", async () => {
      messaging.listRooms.mockResolvedValue({ rooms: [newer, { ...older, lastReadSeq: 5 }], sections: [] });
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => { for (const handler of handlers) handler(available(2, true, { mentionedUserIds: ["user-a"] })); });

      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ lastMessageSeq: 2, unreadCount: 0 });
    });

    it("never moves a room or its preview for a thread reply", async () => {
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));
      const reply = available();
      (reply.payload as { threadRootMessageId: string | null }).threadRootMessageId = "msg-root";

      act(() => { for (const handler of handlers) handler(reply); });

      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ lastMessageSeq: 2, lastMessageAt: older.lastMessageAt });
    });

    it("carries the preview on a top-level hint and swaps it on previewOnly", async () => {
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));
      const preview = {
        messageId: "msg-new", seq: 2, authorId: "user-1", authorKind: "human" as const, authorName: "Maya",
        textPreview: "ship it", hasAttachments: false, createdAt: "2026-09-15T00:00:01Z", threadRootMessageId: null,
      };
      const hint = available();
      (hint.payload as { lastMessage?: unknown }).lastMessage = preview;
      act(() => { for (const handler of handlers) handler(hint); });
      expect(result.current.rooms.find((r) => r.roomId === older.roomId)?.lastMessage).toEqual(preview);

      const edited = available(2, false);
      Object.assign(edited.payload as object, { previewOnly: true, lastMessage: { ...preview, textPreview: "shipped" } });
      act(() => { for (const handler of handlers) handler(edited); });
      expect(result.current.rooms.find((r) => r.roomId === older.roomId))
        .toMatchObject({ lastMessageSeq: 2, unreadCount: 1, lastMessage: { textPreview: "shipped" } });
    });

    it("ignores a stale or replayed hint without moving the room or double-counting", async () => {
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));

      act(() => {
        for (const handler of handlers) handler(available());
        for (const handler of handlers) handler(available());
        for (const handler of handlers) handler(available(1));
      });

      const bumped = result.current.rooms.find((r) => r.roomId === older.roomId);
      expect(bumped).toMatchObject({
        lastMessageSeq: 2, lastMessageAt: "2026-09-15T00:00:01Z", unreadCount: 1,
      });
    });

    it("leaves unread alone for the room on screen — read-marking owns that case", async () => {
      messaging.getRoom.mockImplementation(async (id: string) => (id === older.roomId ? older : newer));
      messaging.listMessages.mockImplementation(async (roomId: string) => ({
        messages: [{ ...message, roomId, messageId: `msg-${roomId}` }],
        nextAfterSeq: null, nextBeforeSeq: null,
      }));
      const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
      await waitFor(() => expect(result.current.rooms).toHaveLength(2));
      act(() => result.current.selectRoom(older.roomId));
      await waitFor(() => expect(result.current.messages).toHaveLength(1));

      act(() => { for (const handler of handlers) handler(available()); });

      const bumped = result.current.rooms.find((r) => r.roomId === older.roomId);
      expect(bumped?.lastMessageSeq).toBe(2);
      expect(bumped?.unreadCount).toBe(0);
    });
  });
});

// A run enters the map from the live `room.run.frame` carrying `run_active`,
// published ONCE at turn start. Open, reload or re-select the room after that
// and the client has no way to learn the agent is working — the room GET does.
describe("runs already in progress when the room opens", () => {
  const AGENT = "agent-1";
  const RUN = "run-open";
  const withRun = (over: Partial<RoomDetail> = {}): RoomDetail => ({
    ...room,
    activeRuns: [{ runId: RUN, agentId: AGENT, triggerMessageId: message.messageId, startedAt: "2026-09-16T18:03:00Z", lastHeartbeatAt: "2026-09-16T18:22:00Z" }],
    ...over,
  });
  const frame = (body: Record<string, unknown>): LiveEvent => ({
    type: "room.run.frame",
    payload: { roomId: room.roomId, agentId: AGENT, runId: RUN, threadRootMessageId: null, frame: body },
  } as LiveEvent);

  it("seeds the room's in-progress run from the room detail", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));

    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    expect(result.current.runs[0]).toMatchObject({
      roomId: room.roomId, agentId: AGENT, runId: RUN, threadRootMessageId: message.messageId, state: "running", text: "", thinking: "", frames: [],
    });
  });

  it.each(["channel", "group-dm", "dm"] as const)("recovers nested thread placement for %s", async (kind) => {
    // A DM needs a recipient besides the signed-in human, or the hook refuses to open it.
    const members = kind === "dm" ? [...room.members, { ...room.members[0], id: "user-b", displayName: "Bob", role: "member" as const }] : room.members;
    messaging.getRoom.mockResolvedValue(withRun({ kind, members }));
    messaging.listMessages.mockResolvedValue({ messages: [{ ...message, threadRootMessageId: "root" }], nextAfterSeq: null });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.runs[0]?.threadRootMessageId).toBe(kind === "dm" ? null : "root"));
  });

  it("keeps a working indicator after an intermediate reply is saved", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    messaging.listMessages.mockResolvedValue({ messages: [message, { ...message, messageId: "progress", messageSeq: 2, runId: RUN,
      author: { kind: "agent", id: AGENT, displayName: "Forge" }, text: "Working on it", threadRootMessageId: message.messageId }], nextAfterSeq: null });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.messages).toHaveLength(2));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    act(() => { for (const handler of handlers) handler(frame({ type: "chunk", runId: RUN, content: "Working on it" })); });
    expect(result.current.runs[0].text).toBe("");
    act(() => { for (const handler of handlers) handler(frame({ type: "run_ended", outcome: "done" })); });
    expect(result.current.runs).toHaveLength(0);
  });

  it("keeps the delegations of a run its saved reply replaced, until the room changes", async () => {
    const other: RoomDetail = { ...room, roomId: "room-2", name: "Second", deliveryKey: "key-2" };
    messaging.listRooms.mockResolvedValue({ rooms: [room, other], sections: [] });
    messaging.getRoom.mockImplementation(async (id: string) => (id === other.roomId ? other : withRun()));
    messaging.listMessages.mockResolvedValue({ messages: [message, { ...message, messageId: "reply", messageSeq: 2, runId: RUN,
      author: { kind: "agent", id: AGENT, displayName: "Forge" }, text: "Started two helpers." }], nextAfterSeq: null });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    act(() => { for (const handler of handlers) {
      handler(frame({ type: "tool_start", runId: RUN, tool: "delegate_task", toolCallId: "t1", args: {} }));
      handler(frame({ type: "tool_end", runId: RUN, tool: "delegate_task", toolCallId: "t1", result: JSON.stringify({ delegation_id: "d-1" }) }));
    } });
    expect(result.current.delegationsByMessage.size).toBe(0); // the live run card still shows it
    act(() => { for (const handler of handlers) handler(frame({ type: "run_ended", outcome: "done" })); });
    expect(result.current.runs).toHaveLength(0);
    expect(result.current.delegationsByMessage.get("reply")).toEqual({ agentId: AGENT, delegationIds: ["d-1"] });

    await act(async () => { result.current.selectRoom(other.roomId); });
    expect(result.current.delegationsByMessage.size).toBe(0);
  });

  it("keeps delegationsByMessage the same Map while a live run streams", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    const before = result.current.delegationsByMessage;
    act(() => { for (const handler of handlers) handler(frame({ type: "chunk", runId: RUN, content: "Working" })); });
    act(() => { for (const handler of handlers) handler(frame({ type: "chunk", runId: RUN, content: "Working on it" })); });
    expect(result.current.runs[0].text).toBe("Working on it");
    expect(result.current.delegationsByMessage).toBe(before);
  });

  it("never retires the run card for a progress line", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    messaging.listMessages.mockResolvedValue({ messages: [message, { ...message, messageId: "progress", messageSeq: 2, runId: RUN, progress: true,
      author: { kind: "agent", id: AGENT, displayName: "Forge" }, text: "Pulled the invoices; totals next." }], nextAfterSeq: null });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.messages).toHaveLength(2));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    act(() => { for (const handler of handlers) handler(frame({ type: "chunk", runId: RUN, content: "Totals match." })); });
    expect(result.current.runs[0].text).toBe("Totals match.");
    act(() => { for (const handler of handlers) handler(frame({ type: "run_ended", outcome: "done" })); });
    expect(result.current.runs).toHaveLength(1);
  });

  it("keeps a stopped run's card when its last streamed text equals a progress line", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    messaging.listMessages.mockResolvedValue({ messages: [message, { ...message, messageId: "progress", messageSeq: 2, runId: RUN, progress: true,
      author: { kind: "agent", id: AGENT, displayName: "Forge" }, text: "Pulled the invoices; totals next." }], nextAfterSeq: null });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.messages).toHaveLength(2));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    act(() => { for (const handler of handlers) handler(frame({ type: "chunk", runId: RUN, content: "Pulled the invoices; totals next." })); });
    act(() => { for (const handler of handlers) handler(frame({ type: "run_ended", outcome: "aborted" })); });
    expect(result.current.runs).toHaveLength(1);
    expect(result.current.runs[0].state).not.toBe("running");
  });

  it("merges a frame that arrives after the seed instead of dropping it", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));

    act(() => { for (const handler of handlers) handler(frame({ type: "chunk", runId: RUN, content: "on it" })); });

    expect(result.current.runs).toHaveLength(1);
    expect(result.current.runs[0]).toMatchObject({ runId: RUN, state: "running", text: "on it" });
  });

  it("never resurrects a run this client already saw end", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    messaging.getRunStatus.mockResolvedValue({ runId: RUN, state: "running" });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));

    act(() => { for (const handler of handlers) handler(frame({ type: "run_ended", runId: RUN, outcome: "done" })); });
    expect(result.current.runs[0]).toMatchObject({ state: "completed" });

    // The ledger read behind this detail raced the run's end, so the refreshed
    // room still lists it.
    messaging.getRoom.mockResolvedValue(withRun({ roomRevision: 2 }));
    await act(async () => { await result.current.refreshRoom(); });

    expect(result.current.runs).toHaveLength(1);
    expect(result.current.runs[0]).toMatchObject({ state: "completed" });
  });

  it("re-checks a seeded run against the ledger, so a stale seed cannot say Working forever", async () => {
    messaging.getRoom.mockResolvedValue(withRun());
    messaging.getRunStatus.mockResolvedValue({ runId: RUN, state: "terminal" });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    expect(messaging.getRunStatus).not.toHaveBeenCalled();

    messaging.getRoom.mockResolvedValue(withRun({ roomRevision: 2 }));
    await act(async () => { await result.current.refreshRoom(); });

    await waitFor(() => expect(result.current.runs[0]).toMatchObject({ state: "completed" }));
    expect(messaging.getRunStatus).toHaveBeenCalledWith(RUN);
  });
});

describe("read and delivery receipts", () => {
  it("keeps receipts for the active room from the detail and live events", async () => {
    messaging.getRoom.mockResolvedValue({
      ...room,
      readCursors: [{ userId: "user-b", lastReadSeq: 2 }],
      agentDeliveries: [{ agentId: "agent-1", lastDeliveredSeq: 1 }],
    });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.receipts.readCursors.get("user-b")).toBe(2));
    expect(result.current.receipts.agentDeliveries.get("agent-1")).toBe(1);

    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-read", type: "room.read.changed", occurredAt: "2026-09-26T00:00:01Z",
      roomId: room.roomId, payload: { roomId: room.roomId, reader: { kind: "human", id: "user-b" }, lastReadSeq: 5 },
    } as LiveEvent); });
    expect(result.current.receipts.readCursors.get("user-b")).toBe(5);

    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-delivery", type: "room.delivery.changed", occurredAt: "2026-09-26T00:00:02Z",
      roomId: room.roomId, payload: { roomId: room.roomId, agentId: "agent-1", lastDeliveredSeq: 3 },
    } as LiveEvent); });
    expect(result.current.receipts.agentDeliveries.get("agent-1")).toBe(3);

    // A delivery event never lower the cursor -- the max is kept.
    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-delivery-stale", type: "room.delivery.changed", occurredAt: "2026-09-26T00:00:03Z",
      roomId: room.roomId, payload: { roomId: room.roomId, agentId: "agent-1", lastDeliveredSeq: 2 },
    } as LiveEvent); });
    expect(result.current.receipts.agentDeliveries.get("agent-1")).toBe(3);

    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-agent-read", type: "room.read.changed", occurredAt: "2026-09-26T00:00:04Z",
      roomId: room.roomId, payload: { roomId: room.roomId, reader: { kind: "agent", id: "agent-1" }, messageId: "m-3" },
    } as LiveEvent); });
    expect(result.current.receipts.agentReads.get("agent-1")?.has("m-3")).toBe(true);
  });

  it("yields empty maps for a detail with no receipt fields (an older cached response)", async () => {
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(result.current.receipts.readCursors.size).toBe(0);
    expect(result.current.receipts.agentDeliveries.size).toBe(0);
    expect(result.current.receipts.agentReads.size).toBe(0);
  });

  it("merges a same-room reseed instead of replacing it: a live agent read and delivery survive a detail refetch that lacks them", async () => {
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));

    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-agent-read-live", type: "room.read.changed", occurredAt: "2026-09-26T00:01:00Z",
      roomId: room.roomId, payload: { roomId: room.roomId, reader: { kind: "agent", id: "agent-1" }, messageId: "m-live" },
    } as LiveEvent); });
    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-delivery-live", type: "room.delivery.changed", occurredAt: "2026-09-26T00:01:01Z",
      roomId: room.roomId, payload: { roomId: room.roomId, agentId: "agent-1", lastDeliveredSeq: 3 },
    } as LiveEvent); });
    expect(result.current.receipts.agentReads.get("agent-1")?.has("m-live")).toBe(true);
    expect(result.current.receipts.agentDeliveries.get("agent-1")).toBe(3);

    // A same-room reseed (the 60s SWR poll, a mutateRoom after markRead, a
    // metadata refetch): the run that produced the live read is no longer
    // open, and the detail's own delivery cursor is stale/lower. Neither may
    // be lost -- `activeRuns` holding only OPEN runs is exactly why a plain
    // replace was wrong.
    messaging.getRoom.mockResolvedValue({
      ...room, roomRevision: 2, activeRuns: [], agentDeliveries: [{ agentId: "agent-1", lastDeliveredSeq: 1 }],
    });
    await act(async () => { await result.current.refreshRoom(); });

    expect(result.current.receipts.agentReads.get("agent-1")?.has("m-live")).toBe(true);
    expect(result.current.receipts.agentDeliveries.get("agent-1")).toBe(3);
  });
});

describe("background subagents", () => {
  const sub = (type: string, seq: number, payload: Record<string, unknown>) => ({ type, seq, payload });
  const emit = (event: LiveEvent) => act(() => { for (const handler of handlers) handler(event); });

  it("room.subagent.event for the active room fills subagents; run_active prunes that agent's finished rows", async () => {
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    emit({ type: "room.subagent.event", payload: { roomId: room.roomId, agentId: "a1", events: [
      sub("subagent.start", 1, { subagent_id: "sa-1", goal: "Map" }),
      sub("subagent.complete", 2, { subagent_id: "sa-2", goal: "Done", status: "completed" }),
    ] } } as LiveEvent);
    expect(result.current.subagents.items.map((i) => i.id)).toEqual(["sa-1", "sa-2"]);
    emit({ type: "room.run.frame", payload: { roomId: room.roomId, agentId: "a1", runId: "run-x", threadRootMessageId: null,
      frame: { type: "run_active", active: true } } } as LiveEvent);
    expect(result.current.subagents.items.map((i) => i.id)).toEqual(["sa-1"]);
  });

  it("snapshots on room open, polls every 5 s only while visible with a live row, and stops after 3 failures", async () => {
    vi.useFakeTimers();
    try {
      messaging.listSubagents.mockResolvedValue({ subagents: [{ subagent_id: "sa-1", agent_id: "a1", goal: "g", status: "running" }] });
      const { result } = renderHook(() => useRooms(platform(), { subagents: true }), { wrapper });
      act(() => result.current.selectRoom(room.roomId));
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(1);
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(1);
      act(() => result.current.subagentsPollVisible(true));
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(2);
      messaging.listSubagents.mockRejectedValue(new Error("503"));
      await act(() => vi.advanceTimersByTimeAsync(20_000));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("takes no snapshot until the subagents option turns on, and none again after it turns off", async () => {
    vi.useFakeTimers();
    try {
      messaging.listSubagents.mockResolvedValue({ subagents: [] });
      const { result, rerender } = renderHook(({ on }) => useRooms(platform(), { subagents: on }), { wrapper, initialProps: { on: false } });
      act(() => result.current.selectRoom(room.roomId));
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(messaging.listSubagents).not.toHaveBeenCalled();
      rerender({ on: true });
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(1);
      rerender({ on: false });
      act(() => result.current.subagentsPollVisible(true));
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("skips a poll tick while the previous snapshot is still in flight", async () => {
    vi.useFakeTimers();
    try {
      messaging.listSubagents.mockResolvedValue({ subagents: [{ subagent_id: "sa-1", agent_id: "a1", goal: "g", status: "running" }] });
      const { result } = renderHook(() => useRooms(platform(), { subagents: true }), { wrapper });
      act(() => result.current.selectRoom(room.roomId));
      await act(() => vi.advanceTimersByTimeAsync(0));
      act(() => result.current.subagentsPollVisible(true));
      messaging.listSubagents.mockImplementation(() => new Promise(() => {})); // never answers
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(2);
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      expect(messaging.listSubagents).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("an instance that did not opt in never asks the runtime for a snapshot", async () => {
    cleanup(); // an earlier opted-in hook would answer the same events
    vi.useFakeTimers();
    try {
      messaging.listSubagents.mockResolvedValue({ subagents: [{ subagent_id: "sa-1", agent_id: "a1", goal: "g", status: "running" }] });
      const { result } = renderHook(() => useRooms(platform()), { wrapper });
      act(() => result.current.selectRoom(room.roomId));
      act(() => result.current.subagentsPollVisible(true));
      emit({ type: "room.subagent.event", payload: { roomId: room.roomId, agentId: "a1", events: [
        sub("subagent.start", 1, { subagent_id: "sa-1", goal: "Map" }),
      ] } } as LiveEvent);
      await act(() => vi.advanceTimersByTimeAsync(20_000));
      act(() => { for (const listener of visibilityListeners) listener(); });
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(messaging.listSubagents).not.toHaveBeenCalled();
      expect(result.current.subagents.items.map((i) => i.id)).toEqual(["sa-1"]); // live events still apply
    } finally {
      vi.useRealTimers();
    }
  });

  it("becoming visible again refreshes only while the strip is visible, and resets the failure stop", async () => {
    cleanup(); // earlier hooks in this file are never unmounted and would answer the same visibility change
    messaging.listSubagents.mockResolvedValue({ subagents: [] });
    const { result } = renderHook(() => useRooms(platform(), { subagents: true }), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(messaging.listSubagents).toHaveBeenCalled());
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    const settled = messaging.listSubagents.mock.calls.length;
    const regain = () => act(() => { for (const listener of visibilityListeners) listener(); });
    regain();
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    expect(messaging.listSubagents).toHaveBeenCalledTimes(settled);
    act(() => result.current.subagentsPollVisible(true));
    visible = false;
    regain(); // a visibility LOSS is not a refresh
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    expect(messaging.listSubagents).toHaveBeenCalledTimes(settled);
    visible = true;
    regain();
    await waitFor(() => expect(messaging.listSubagents).toHaveBeenCalledTimes(settled + 1));
  });
});
