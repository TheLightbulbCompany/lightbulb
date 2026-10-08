// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import type { CanonicalRoomMessage, LiveEvent, MessagePage, RoomDetail, RoomListResponse } from "../index";
import {
  useRooms,
  type RoomsLiveSource,
  type RoomsPersistence,
  type RoomsPlatform,
  type RoomsSnapshot,
} from "./useRooms";

// The persistence seam: a cold start paints the last-known list and recent
// history from `load`, and every later change goes back out through `save`.

const summary = (roomId: string): RoomDetail => ({
  roomId, name: roomId, kind: "channel", roomRevision: 1, deliveryKey: `key-${roomId}`,
  lastMessageSeq: 1, lastMessageAt: "2026-09-11T00:00:00Z", unreadCount: 0,
  updatedAt: "2026-09-11T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null,
  lastReadSeq: 1, createdAt: "2026-09-11T00:00:00Z", pinnedMessageIds: [], members: [],
});
const msg = (roomId: string, seq: number): CanonicalRoomMessage => ({
  messageId: `${roomId}-m${seq}`, roomId, messageSeq: seq, roomRevision: 1,
  author: { kind: "human", id: "user-b", displayName: "Bob" }, text: `m${seq}`,
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: "2026-09-11T00:00:00Z", runId: null,
});
const list = (...ids: string[]): RoomListResponse => ({ rooms: ids.map(summary), sections: [] });
const snapshot = (ids: string[], history: string[] = ids): RoomsSnapshot => ({
  rooms: list(...ids),
  history: history.map((roomId) => ({ roomId, messages: [msg(roomId, 1)], complete: true })),
});
const never = () => new Promise<never>(() => {});

const handlers = new Set<(event: LiveEvent) => void>();
const messaging = {
  listRooms: vi.fn<() => Promise<RoomListResponse>>(),
  getRoom: vi.fn<(roomId: string) => Promise<RoomDetail>>(),
  listMessages: vi.fn<(roomId: string, afterSeq: number) => Promise<MessagePage>>(),
  markRead: vi.fn(async () => ({ roomId: "x", lastReadSeq: 1 })),
  getRunStatus: vi.fn(),
};
const live: RoomsLiveSource = {
  messaging: messaging as never,
  onEvent: (handler) => { handlers.add(handler); return () => handlers.delete(handler); },
  claimActiveRoomSubscription: vi.fn(() => vi.fn()),
  liveChannels: [], connectionRevision: 1, liveConnected: false, httpReady: true,
};
const onVisibilityChange = () => () => {};
const isVisible = () => true;
const uuid = () => "11111111-1111-4111-8111-111111111111";
const platform = (over: Partial<RoomsPlatform> = {}): RoomsPlatform => ({
  userId: "user-a", orgId: "org-a", live, isVisible, onVisibilityChange, uuid, ...over,
});
const wrapper = ({ children }: { children: ReactNode }) =>
  <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;

/** An in-memory stand-in for the phone's file store. */
const fakeStore = (files: Record<string, RoomsSnapshot> = {}) => {
  const persistence: RoomsPersistence & { saved: RoomsSnapshot[] } = {
    saved: [],
    load: vi.fn((scope: string) => files[scope] ?? null),
    save: vi.fn((scope: string, next: RoomsSnapshot) => { files[scope] = next; persistence.saved.push(next); }),
  };
  return persistence;
};
const lastSaved = (p: { saved: RoomsSnapshot[] }) => p.saved[p.saved.length - 1];
const savedIds = (p: { saved: RoomsSnapshot[] }) => ({
  rooms: lastSaved(p)?.rooms.rooms.map((room) => room.roomId),
  history: lastSaved(p)?.history.map((entry) => entry.roomId),
});

beforeEach(() => {
  handlers.clear();
  for (const fn of Object.values(messaging)) fn.mockReset();
  messaging.getRoom.mockImplementation(async (roomId) => summary(roomId));
  messaging.listMessages.mockImplementation(async (roomId) => ({ messages: [msg(roomId, 1)], nextAfterSeq: null, nextBeforeSeq: null }));
  messaging.markRead.mockResolvedValue({ roomId: "x", lastReadSeq: 1 });
});

describe("useRooms persistence", () => {
  it("paints the persisted list and history before the network answers", async () => {
    messaging.listRooms.mockImplementation(never);
    messaging.listMessages.mockImplementation(never);
    messaging.getRoom.mockImplementation(never);
    const persistence = fakeStore({ "user-a:org-a": snapshot(["room-1"]) });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false, persistence }), { wrapper });
    expect(result.current.rooms.map((room) => room.roomId)).toEqual(["room-1"]);
    expect(result.current.roomsLoading).toBe(false);
    act(() => result.current.selectRoom("room-1"));
    expect(result.current.messages.map((m) => m.messageId)).toEqual(["room-1-m1"]);
    // ...and still reconciles: the delta behind the cached history is asked for.
    await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith("room-1", 1, 100, expect.anything()));
    // SWR revalidates a list it already has on the next frame, not synchronously.
    await waitFor(() => expect(messaging.listRooms).toHaveBeenCalled());
  });

  it.each([
    ["another user", { userId: "user-b" }, "user-b:org-a"],
    ["another workspace", { orgId: "org-b" }, "user-a:org-b"],
  ])("reads only its own scope, never %s's", (_label, over, scope) => {
    messaging.listRooms.mockImplementation(never);
    const persistence = fakeStore({ "user-a:org-a": snapshot(["room-1"]) });
    const { result } = renderHook(() => useRooms(platform(over), { autoSelect: false, persistence }), { wrapper });
    expect(persistence.load).toHaveBeenCalledTimes(1);
    expect(persistence.load).toHaveBeenCalledWith(scope);
    expect(result.current.rooms).toHaveLength(0);
  });

  it("reads nothing while signed out", () => {
    const persistence = fakeStore({ "user-a:org-a": snapshot(["room-1"]) });
    renderHook(() => useRooms(platform({ userId: null }), { persistence }), { wrapper });
    expect(persistence.load).not.toHaveBeenCalled();
  });

  it("rewrites the snapshot when a fresh list drops a room", async () => {
    messaging.listRooms.mockResolvedValue(list("room-1"));
    const persistence = fakeStore({ "user-a:org-a": snapshot(["room-1", "room-2"]) });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false, persistence }), { wrapper });
    await waitFor(() => expect(result.current.rooms).toHaveLength(1));
    await waitFor(() => expect(savedIds(persistence)).toEqual({ rooms: ["room-1"], history: ["room-1"] }));
  });

  it("drops a room from the snapshot on room.access.removed", async () => {
    // The list never answers: the event arrives while the painted snapshot is what is on screen.
    // This used to resolve the first list and wait only for the first save, which is the
    // snapshot's own, on mount: the event then raced the list. It passed when the event came
    // first and failed when the list had landed (CI, 2026-10-08), because a list that has
    // landed keeps the removed room on screen until its refetch answers.
    messaging.listRooms.mockImplementation(never);
    const persistence = fakeStore({ "user-a:org-a": snapshot(["room-1", "room-2"]) });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false, persistence }), { wrapper });
    await waitFor(() => expect(persistence.save).toHaveBeenCalled());
    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-removed", type: "room.access.removed", occurredAt: "2026-09-15T00:00:01Z",
      roomId: "room-2", payload: { roomId: "room-2", roomRevision: 2 },
    } as LiveEvent); });
    // The list refetch is held open: neither the snapshot nor the screen waits for it.
    expect(savedIds(persistence)).toEqual({ rooms: ["room-1"], history: ["room-1"] });
    await waitFor(() => expect(result.current.rooms.map((room) => room.roomId)).toEqual(["room-1"]));
  });

  it("drops a removed room from the painted snapshot list before the network answers", async () => {
    messaging.listRooms.mockImplementation(never);
    const persistence = fakeStore({ "user-a:org-a": snapshot(["room-1", "room-2"]) });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false, persistence }), { wrapper });
    expect(result.current.rooms).toHaveLength(2);
    act(() => { for (const handler of handlers) handler({
      schemaVersion: 1, eventId: "evt-removed-early", type: "room.access.removed", occurredAt: "2026-09-15T00:00:01Z",
      roomId: "room-2", payload: { roomId: "room-2", roomRevision: 2 },
    } as LiveEvent); });
    await waitFor(() => expect(result.current.rooms.map((room) => room.roomId)).toEqual(["room-1"]));
  });

  it("saves history as it loads, capped to the newest messages of the most recent rooms", async () => {
    const ids = Array.from({ length: 12 }, (_, i) => `room-${i}`);
    messaging.listRooms.mockResolvedValue(list(...ids));
    messaging.listMessages.mockImplementation(async (roomId) => ({
      messages: Array.from({ length: 60 }, (_, i) => msg(roomId, i + 1)), nextAfterSeq: null, nextBeforeSeq: null,
    }));
    const persistence = fakeStore();
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false, persistence }), { wrapper });
    await waitFor(() => expect(result.current.rooms).toHaveLength(12));
    for (const roomId of ids) {
      act(() => result.current.selectRoom(roomId));
      await waitFor(() => expect(result.current.messages).toHaveLength(60));
    }
    const saved = lastSaved(persistence);
    expect(saved.history.map((entry) => entry.roomId)).toEqual(ids.slice(-10));
    expect(saved.history.every((entry) => entry.messages.length === 50 && !entry.complete)).toBe(true);
    expect(saved.history[0].messages[0].messageSeq).toBe(11);
  });

  it("changes nothing without an adapter: the list loads from the network", () => {
    messaging.listRooms.mockImplementation(never);
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    expect(result.current.rooms).toHaveLength(0);
    expect(result.current.roomsLoading).toBe(true);
  });
});
