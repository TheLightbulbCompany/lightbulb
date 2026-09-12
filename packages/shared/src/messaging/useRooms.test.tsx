// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import type { CanonicalRoomMessage, LiveEvent, RoomDetail, RoomSection } from "../index";
import { useRooms, type RoomsLiveSource, type RoomsPlatform } from "./useRooms";

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
  getRoom: vi.fn(async () => room),
  listMessages: vi.fn(async () => ({ messages: [message], nextAfterSeq: null as number | null })),
  markRead: vi.fn(async () => ({ roomId: room.roomId, lastReadSeq: 1 })),
  uploadAttachment: vi.fn(async () => ({ id: "att-1", fileName: "a.png", contentType: "image/png" as const, size: 1 })),
  getRunStatus: vi.fn(async () => ({ runId: "run-1", state: "terminal" as const })),
  markUnread: vi.fn(), sendMessage: vi.fn(), setTyping: vi.fn(), createRoom: vi.fn(),
  updateRoom: vi.fn(), upsertMember: vi.fn(), removeMember: vi.fn(), abortRun: vi.fn(),
  steerRun: vi.fn(), updateMessage: vi.fn(), setReaction: vi.fn(), downloadAttachment: vi.fn(),
  getLiveConfig: vi.fn(), listResources: vi.fn(),
  setMemberPrefs: vi.fn(), putSections: vi.fn(), leaveRoom: vi.fn(), setPin: vi.fn(),
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
  // History only reconciles once the room's own channel is live — the same
  // path the web suite feeds its fixture.
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
  handlers.clear();
  visibilityListeners.clear();
  for (const fn of Object.values(messaging)) fn.mockClear();
  messaging.listRooms.mockResolvedValue({ rooms: [room], sections: [] });
  messaging.getRoom.mockResolvedValue(room);
  messaging.listMessages.mockResolvedValue({ messages: [message], nextAfterSeq: null });
});

describe("useRooms platform seams", () => {
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

  it("uploads a DOM Blob and a react-native file descriptor through the same call", async () => {
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    const blob = new Blob(["x"], { type: "image/png" });
    const native = { uri: "file:///tmp/a.png", name: "a.png", type: "image/png" };
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

  it("treats a null userId as signed out and fetches nothing", async () => {
    renderHook(() => useRooms(platform({ userId: null })), { wrapper });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(messaging.listRooms).not.toHaveBeenCalled();
  });
});
