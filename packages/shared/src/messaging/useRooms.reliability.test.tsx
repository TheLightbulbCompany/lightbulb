// @vitest-environment jsdom
import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig, unstable_serialize } from "swr";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLiveEvents } from "../live/useLiveEvents";
import type { CanonicalRoomMessage, LiveEvent, MessagePage, RoomDetail, RoomSection, RunStatusResponse } from "../index";
import { createMessagingClient } from "./client";
import type { ApiCallOptions, ApiMethods } from "../api/client";
import { ApiError } from "../api/error";
import { useRooms as useSharedRooms, type RoomsLiveSource, type RoomsPlatform, type UseRoomsOptions } from "./useRooms";

const useRooms = (platform: RoomsPlatform, options: UseRoomsOptions = {}) =>
  useSharedRooms(platform, { boundedHistory: true, ...options });

const room: RoomDetail = {
  roomId: "room-1", workspaceId: "ws-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "key-1",
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
  steerRun: vi.fn(), updateMessage: vi.fn(), setReaction: vi.fn(), downloadAttachment: vi.fn(),
  getLiveConfig: vi.fn(),
  setMemberPrefs: vi.fn(), putSections: vi.fn(), leaveRoom: vi.fn(), setPin: vi.fn(),
};

let visible = true;
const visibilityListeners = new Set<() => void>();
// Provider callbacks keep stable identities; channel arrays may be re-emitted.
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
});


class FakeSocket {
  static instances: FakeSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = FakeSocket.CONNECTING;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string, public protocols: string[]) {
    FakeSocket.instances.push(this);
  }

  send(raw: string) {
    this.sent.push(JSON.parse(raw));
  }

  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  receive(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  close() {
    if (this.readyState === FakeSocket.CLOSED) return;
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }

  ack() {
    this.receive({ type: "connection_ack", connectionTimeoutMs: 60_000 });
  }

  subscriptions() {
    return this.sent.filter((message) => message.type === "subscribe");
  }
}


const USER_CHANNEL = "/ws/ws-1/users/user-key";
const channel = (key = "key-1") => `/ws/ws-1/rooms/${room.roomId}/${key}`;
const config = { schemaVersion: 1 as const, httpEndpoint: "https://events.appsync-api.us-east-1.amazonaws.com/event", realtimeEndpoint: "wss://events.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime", region: "us-east-1", workspaceId: "ws-1", userChannelKey: "user-key" };
let subId = 0;
const socketUuid = () => `sub-${++subId}`;
const dispatch = (event: LiveEvent) => { for (const handler of handlers) handler(event); };
function useCombinedHarness() {
  const [claimed, setClaimed] = useState<{roomId: string; deliveryKey: string} | null>(null);
  const claim = useCallback((next: {roomId: string; deliveryKey: string}) => {
    setClaimed(next);
    return () => setClaimed(null);
  }, []);
  const transport = useLiveEvents({ config, userChannel: USER_CHANNEL,
    roomChannel: claimed ? channel(claimed.deliveryKey) : null,
    getToken: async () => "fixture-token", uuid: socketUuid, onEvent: dispatch });
  const source = useMemo<RoomsLiveSource>(() => ({...live, messaging: messaging as never,
    claimActiveRoomSubscription: claim, liveChannels: transport.liveChannels,
    connectionRevision: transport.connectionRevision, liveConnected: transport.liveConnected,
  }), [claim, transport.liveChannels, transport.connectionRevision, transport.liveConnected]);
  const rooms = useRooms(platform({live: source}));
  return {rooms, transport};
}
async function drain() { await act(async () => { await Promise.resolve(); }); }
function ackChannel(ws: FakeSocket, path: string) {
  const sub = ws.subscriptions().reverse().find(s => s.channel === path);
  if (!sub) throw Error(`No subscription for ${path}`);
  ws.receive({type: "subscribe_success", id: sub.id});
}
function deliver(ws: FakeSocket, path: string, type: string, payload: Record<string, unknown>) {
  const sub = ws.subscriptions().reverse().find(s => s.channel === path)!;
  ws.receive({type: "data", id: sub.id, event: JSON.stringify({schemaVersion: 1,
    eventId: `event-${++subId}`, occurredAt: message.createdAt, roomId: room.roomId,
    messageSeq: payload.messageSeq, roomRevision: payload.roomRevision, type, payload})});
}
const second = {...message, messageId: "msg-2", messageSeq: 2, text: "persisted during gap"};
const numbered = (seq: number): CanonicalRoomMessage => ({ ...message, messageId: `msg-${seq}`, messageSeq: seq, text: `Message ${seq}` });
function historyPage(server: CanonicalRoomMessage[], after: number, before?: number): MessagePage {
  if (before !== undefined) {
    const older = server.filter(m => m.messageSeq < before);
    const messages = older.slice(-100);
    return { messages, nextAfterSeq: null, nextBeforeSeq: older.length > 100 ? messages[0].messageSeq : null };
  }
  const newer = server.filter(m => m.messageSeq > after);
  const messages = newer.slice(0, 100);
  return { messages, nextAfterSeq: newer.length > 100 ? messages.at(-1)!.messageSeq : null };
}
function longRoom(server: CanonicalRoomMessage[]) {
  const detail = { ...room, lastMessageSeq: server.at(-1)?.messageSeq ?? 0 };
  messaging.listRooms.mockResolvedValue({ rooms: [detail], sections: [] });
  messaging.getRoom.mockResolvedValue(detail);
  messaging.listMessages.mockImplementation(async (_roomId, after, _limit, options) => historyPage(server, after, options?.beforeSeq));
}
const created = (payload: CanonicalRoomMessage): LiveEvent => ({
  schemaVersion: 1, eventId: `created-${payload.messageId}`, occurredAt: message.createdAt,
  roomId: payload.roomId, type: "room.message.created", payload,
});

beforeEach(() => {
  visible = false;
  live.liveConnected = false;
  live.connectionRevision = 1;
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("room history reliability with real live transport", () => {
  it("opens an online long room with only its newest page, then pages older without gaps", async () => {
    const server = Array.from({ length: 201 }, (_, i) => numbered(i + 1));
    longRoom(server);
    live.liveConnected = true;
    live.liveChannels = [channel()];
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages.map(m => m.messageSeq)).toEqual(server.slice(-100).map(m => m.messageSeq)));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    expect(result.current.historyComplete).toBe(false);
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { beforeSeq: 202, signal: expect.any(AbortSignal) });
    expect(messaging.listMessages.mock.calls.some(([, after, , options]) => after === 0 && options?.beforeSeq === undefined)).toBe(false);
    await act(async () => result.current.loadOlder());
    expect(result.current.messages.map(m => m.messageSeq)).toEqual(server.slice(1).map(m => m.messageSeq));
    expect(result.current.historyComplete).toBe(false);
    await act(async () => result.current.loadOlder());
    expect(result.current.messages.map(m => m.messageSeq)).toEqual(server.map(m => m.messageSeq));
    expect(result.current.historyComplete).toBe(true);
  });

  it("keeps older paging contiguous when reactions arrive outside the loaded window", async () => {
    const server = Array.from({ length: 201 }, (_, i) => numbered(i + 1));
    longRoom(server);
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages).toHaveLength(100));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    for (const index of [0, 200]) {
      server[index] = { ...server[index], reactions: { "👍": ["user-b"] }, reactionRevision: 1 };
      act(() => dispatch({ ...created(server[index]), type: "room.message.reactions.changed", payload: server[index] }));
    }
    expect(result.current.messages.find(m => m.messageSeq === 201)?.reactions).toEqual({ "👍": ["user-b"] });
    await act(async () => result.current.loadOlder());
    await act(async () => result.current.loadOlder());
    expect(result.current.messages.map(m => m.messageSeq)).toEqual(server.map(m => m.messageSeq));
    expect(result.current.messages[0].reactions).toEqual({ "👍": ["user-b"] });
    expect(result.current.historyComplete).toBe(true);
  });

  it("falls back to full history while an older backend ignores beforeSeq", async () => {
    messaging.listMessages.mockResolvedValue({ messages: [message], nextAfterSeq: null });
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.historyComplete).toBe(true));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) });
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, { signal: expect.any(AbortSignal) });
  });

  it("merges live arrivals during initialization and catches the gap from the initial page watermark", async () => {
    const server = Array.from({ length: 201 }, (_, i) => numbered(i + 1));
    longRoom(server);
    let finish!: (page: MessagePage) => void;
    const initial = historyPage(server, 0, 202);
    messaging.listMessages.mockImplementation((_id, after, _limit, options) => options?.beforeSeq !== undefined
      ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(historyPage(server, after)));
    live.liveConnected = true;
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledTimes(1));
    server.push(numbered(202), numbered(203));
    act(() => dispatch(created(server[202])));
    expect(result.current.messages.map(m => m.messageSeq)).toEqual([203]);
    act(() => dispatch(created({ ...server[200], text: "Updated during initial read", messageRevision: 2 })));
    await act(async () => finish(initial));
    await waitFor(() => expect(result.current.messages.map(m => m.messageSeq)).toEqual(server.slice(101).map(m => m.messageSeq)));
    expect(result.current.messages.find(m => m.messageSeq === 201)?.text).toBe("Updated during initial read");
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 201, 100, { signal: expect.any(AbortSignal) });
    expect(result.current.historyComplete).toBe(false);
  });

  it("catches a live gap while an older page is pending and keeps loading until both finish", async () => {
    const server = Array.from({ length: 201 }, (_, i) => numbered(i + 1));
    longRoom(server);
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages).toHaveLength(100));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    let finishOlder!: (page: MessagePage) => void;
    messaging.listMessages.mockImplementation((_id, after, _limit, options) => options?.beforeSeq !== undefined
      ? new Promise(resolve => { finishOlder = resolve; }) : Promise.resolve(historyPage(server, after)));
    let older!: Promise<void>;
    act(() => { older = result.current.loadOlder(); });
    server.push(numbered(202), numbered(203));
    await act(async () => dispatch(created(server[202])));
    expect(result.current.historyLoading).toBe(true);
    await act(async () => { finishOlder(historyPage(server, 0, 102)); await older; });
    await waitFor(() => expect(result.current.messages.map(m => m.messageSeq)).toEqual(server.slice(1).map(m => m.messageSeq)));
    expect(result.current.historyLoading).toBe(false);
    expect(result.current.historyComplete).toBe(false);
  });

  it("reconciles edits announced while the initial HTTP snapshot is still pending", async () => {
    const server = Array.from({ length: 201 }, (_, i) => numbered(i + 1));
    longRoom(server);
    const initial = historyPage(server, 0, 202);
    let finish!: (page: MessagePage) => void;
    messaging.listMessages.mockImplementation((_id, after, _limit, options) => options?.beforeSeq !== undefined
      ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(historyPage(server, after)));
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledTimes(1));
    server[149] = { ...server[149], text: "Edited after snapshot", messageRevision: 2 };
    act(() => dispatch({ schemaVersion: 1, eventId: "edit-during-initial", occurredAt: message.createdAt,
      roomId: room.roomId, type: "room.message.updated", payload: { roomId: room.roomId, messageId: "msg-150", messageSeq: 150 } }));
    await act(async () => finish(initial));
    await waitFor(() => expect(result.current.messages.find(m => m.messageSeq === 150)?.text).toBe("Edited after snapshot"));
  });

  it("refetches session receipts when room metadata advances without a live event", async () => {
    let server = message;
    let detail: RoomDetail = { ...room, sessionSendsRevision: 0 };
    messaging.getRoom.mockImplementation(async () => detail);
    messaging.listMessages.mockImplementation(async (_id, afterSeq) => ({
      messages: afterSeq === 0 ? [server] : [], nextAfterSeq: null, nextBeforeSeq: null,
    }));
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    server = { ...message, sessionSends: [{ sessionId: "s1", requestId: "r1" }] };
    detail = { ...detail, sessionSendsRevision: 1 };
    await act(async () => { await result.current.refreshRoom(); });
    await waitFor(() => expect(result.current.messages[0]?.sessionSends).toEqual([{ sessionId: "s1", requestId: "r1" }]));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 0, 100, expect.anything());
  });

  it("supersedes an older read with explicit full history without a late cancellation error", async () => {
    const server = Array.from({ length: 201 }, (_, i) => numbered(i + 1));
    longRoom(server);
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages).toHaveLength(100));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    let finishOlder!: (page: MessagePage) => void;
    let olderSignal: AbortSignal | undefined;
    const get = vi.fn((path: string, options?: ApiCallOptions) => {
      const url = new URL(path, "https://example.test");
      if (url.searchParams.has("beforeSeq")) {
        olderSignal = options?.signal;
        return new Promise(resolve => { finishOlder = resolve; });
      }
      return Promise.resolve(historyPage(server, Number(url.searchParams.get("afterSeq"))));
    });
    messaging.listMessages.mockImplementation(createMessagingClient({ get } as unknown as ApiMethods).listMessages);
    act(() => { void result.current.loadOlder(); });
    await act(async () => result.current.loadHistory());
    await waitFor(() => expect(result.current.historyComplete).toBe(true));
    expect(olderSignal?.aborted).toBe(true);
    expect(result.current.messages).toHaveLength(201);
    expect(result.current.historyError).toBe(false);
    await act(async () => finishOlder(historyPage(server, 0, 102)));
    expect(result.current.historyComplete).toBe(true);
    expect(result.current.historyError).toBe(false);
    expect(result.current.historyLoading).toBe(false);
  });

  it("restores a partial cached window without silently loading all older history", async () => {
    const server = Array.from({ length: 201 }, (_, i) => numbered(i + 1));
    longRoom(server);
    const other = { ...room, roomId: "room-other" };
    messaging.getRoom.mockImplementation(async id => id === other.roomId ? other : { ...room, lastMessageSeq: 201 });
    messaging.listMessages.mockImplementation(async (id, after, _limit, options) => id === other.roomId
      ? { messages: [], nextAfterSeq: null, nextBeforeSeq: null } : historyPage(server, after, options?.beforeSeq));
    const { result } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.messages).toHaveLength(100));
    await act(async () => result.current.selectRoom(other.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(other.roomId));
    messaging.listMessages.mockClear();
    await act(async () => result.current.selectRoom(room.roomId));
    expect(result.current.messages).toHaveLength(100);
    expect(result.current.historyComplete).toBe(false);
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 201, 100, { signal: expect.any(AbortSignal) });
    expect(messaging.listMessages.mock.calls.some(([, after]) => after === 0)).toBe(false);
  });

  it.each(["room", "scope", "unmount", "access"])("cancels older paging on %s change without restoring old rows", async change => {
    const server = Array.from({ length: 101 }, (_, i) => numbered(i + 1));
    longRoom(server);
    const { result, rerender, unmount } = renderHook(({ userId }) => useRooms(platform({ userId })), { wrapper, initialProps: { userId: "user-a" } });
    await waitFor(() => expect(result.current.messages).toHaveLength(100));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    const pending: Array<{ signal?: AbortSignal; resolve: (page: unknown) => void }> = [];
    const get = vi.fn((_path: string, options?: ApiCallOptions) => new Promise(resolve => pending.push({ signal: options?.signal, resolve })));
    messaging.listMessages.mockImplementation(createMessagingClient({ get } as unknown as ApiMethods).listMessages);
    act(() => { void result.current.loadOlder(); });
    expect(pending).toHaveLength(1);
    if (change === "unmount") unmount();
    else if (change === "scope") rerender({ userId: "other-user" });
    else if (change === "room") act(() => result.current.selectRoom("other-room"));
    else act(() => dispatch({ schemaVersion: 1, eventId: "revoked", occurredAt: message.createdAt,
      roomId: room.roomId, type: "room.access.removed", payload: { roomId: room.roomId, roomRevision: 2 } } as LiveEvent));
    await drain();
    expect(pending[0].signal?.aborted).toBe(true);
    await act(async () => pending[0].resolve(historyPage(server, 0, 2)));
    if (change !== "unmount") {
      expect(result.current.messages).toEqual([]);
      expect(result.current.historyError).toBe(false);
      if (change === "access") expect(result.current.accessRemoved).toBe(true);
      unmount();
    }
  });

  it("preserves a child link selection when cached rooms trigger initial auto-selection", async () => {
    const linkedRoom = { ...room, roomId: "room-linked" };
    const directory = { rooms: [room, linkedRoom], sections: [] };
    messaging.listRooms.mockResolvedValue(directory);
    messaging.getRoom.mockImplementation(async roomId => roomId === linkedRoom.roomId ? linkedRoom : room);
    messaging.listMessages.mockImplementation(async roomId => ({ messages: [{ ...message, roomId }], nextAfterSeq: null }));
    const cache = new Map([[unstable_serialize(["messaging-rooms", "user-a:org-a"]), { data: directory }]]);
    function RoomLink({ selectRoom }: { selectRoom: (roomId: string) => void }) {
      useEffect(() => { selectRoom(linkedRoom.roomId); }, [selectRoom]);
      return null;
    }
    function Parent() {
      const rooms = useRooms(platform());
      return <><RoomLink selectRoom={rooms.selectRoom} /><output aria-label="Active room">{rooms.activeRoomId}</output></>;
    }
    render(<SWRConfig value={{ provider: () => cache, dedupingInterval: 0 }}><Parent /></SWRConfig>);
    await waitFor(() => expect(screen.getByLabelText("Active room").textContent).toBe(linkedRoom.roomId));
    await waitFor(() => expect(messaging.listMessages).toHaveBeenCalledWith(linkedRoom.roomId, 0, 100, { beforeSeq: 2, signal: expect.any(AbortSignal) }));
    expect(messaging.listMessages).not.toHaveBeenCalledWith(room.roomId, expect.anything(), expect.anything(), expect.anything());
  });

  it("catches messages missed before the initial room ACK, and after reconnect", async () => {
    let server = [message];
    messaging.listMessages.mockImplementation(async (_id, after) => ({messages: server.filter(m => m.messageSeq > after), nextAfterSeq: null}));
    const {result, unmount} = renderHook(useCombinedHarness, {wrapper});
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    const ws = FakeSocket.instances[0];
    await act(async () => { ws.open(); ws.ack(); });
    await waitFor(() => expect(ws.subscriptions().some(s => s.channel === channel())).toBe(true));
    await act(async () => ackChannel(ws, USER_CHANNEL));
    await waitFor(() => expect(result.current.rooms.historyLoading).toBe(false));
    expect(result.current.rooms.messages.map(m => m.messageSeq)).toEqual([1]);
    expect(result.current.transport.roomSubscriptionPending).toBe(true);
    expect(result.current.transport.liveConnected).toBe(true);
    const revision = result.current.transport.connectionRevision;
    messaging.listMessages.mockClear(); messaging.listRooms.mockClear();
    server = [message, second];
    await act(async () => {
      deliver(ws, USER_CHANNEL, "room.message.available", {roomId: room.roomId, messageId: second.messageId, messageSeq: 2, threadRootMessageId: null, notify: true});
      ackChannel(ws, channel());
    });
    await waitFor(() => expect(messaging.listRooms).toHaveBeenCalled());
    expect(result.current.transport.roomSubscriptionPending).toBe(false);
    expect(result.current.transport.connectionRevision).toBe(revision);
    await waitFor(() => expect(result.current.rooms.messages.map(m => m.messageSeq)).toEqual([1, 2]));
    act(() => ws.close());
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(2));
    const recovered = FakeSocket.instances[1];
    await act(async () => { recovered.open(); recovered.ack(); });
    await waitFor(() => expect(recovered.subscriptions().some(s => s.channel === USER_CHANNEL)).toBe(true));
    await act(async () => { ackChannel(recovered, USER_CHANNEL); ackChannel(recovered, channel()); });
    await waitFor(() => expect(result.current.rooms.messages.map(m => m.messageSeq)).toEqual([1,2]));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 1, 100, { signal: expect.any(AbortSignal) });
    unmount();
  });

  it("catches messages missed during delivery-key rotation before any later message", async () => {
    let server = [message];
    messaging.listMessages.mockImplementation(async (_id, after) => ({messages: server.filter(m => m.messageSeq > after), nextAfterSeq: null}));
    const {result, unmount} = renderHook(useCombinedHarness, {wrapper});
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    const ws = FakeSocket.instances[0];
    await act(async () => { ws.open(); ws.ack(); });
    await waitFor(() => expect(ws.subscriptions().some(s => s.channel === channel())).toBe(true));
    await act(async () => { ackChannel(ws, USER_CHANNEL); ackChannel(ws, channel()); });
    await waitFor(() => expect(result.current.rooms.historyLoading).toBe(false));
    expect(result.current.rooms.messages.map(m => m.messageSeq)).toEqual([1]);
    messaging.listMessages.mockClear();
    server = [message, second];
    messaging.getRoom.mockResolvedValue({...room, deliveryKey:"key-2", roomRevision:2, lastMessageSeq:2});
    await act(async () => deliver(ws, USER_CHANNEL, "room.subscription.rotated", {roomId: room.roomId, deliveryKey:"key-2", roomRevision:2}));
    await waitFor(() => expect(ws.subscriptions().some(s => s.channel === channel("key-2"))).toBe(true));
    expect(result.current.transport.roomSubscriptionPending).toBe(true);
    await act(async () => ackChannel(ws, channel("key-2")));
    expect(result.current.transport.roomSubscriptionPending).toBe(false);
    expect(result.current.rooms.activeRoom?.lastMessageSeq).toBe(2);
    await waitFor(() => expect(result.current.rooms.messages.map(m => m.messageSeq)).toEqual([1, 2]));
    const third = {...message, messageId:"msg-3", messageSeq:3};
    server = [message, second, third];
    await act(async () => deliver(ws, channel("key-2"), "room.message.created", third));
    await waitFor(() => expect(result.current.rooms.messages.map(m => m.messageSeq)).toEqual([1,2,3]));
    expect(messaging.listMessages).toHaveBeenCalledWith(room.roomId, 1, 100, { signal: expect.any(AbortSignal) });
    unmount();
  });

  it("ignores unrelated workspace ACKs and fresh arrays containing the same room channel", async () => {
    live.liveChannels = [];
    live.liveConnected = true;
    const { result, rerender } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    messaging.listMessages.mockClear();
    live.liveChannels = [channel().replace("ws-1", "ws-other")];
    rerender();
    await drain();
    expect(messaging.listMessages).not.toHaveBeenCalled();
    live.liveChannels = [channel()];
    rerender();
    await drain();
    expect(messaging.listMessages).toHaveBeenCalledTimes(1);
    messaging.listMessages.mockClear();
    live.liveChannels = [...live.liveChannels, "/ws/ws-1/users/unrelated"];
    rerender();
    await drain();
    expect(messaging.listMessages).not.toHaveBeenCalled();
  });

  it("times out a hung read, allows Retry and ignores the old response", async () => {
    vi.useFakeTimers();
    let finishOld!: (page: unknown) => void;
    const signals: AbortSignal[] = [];
    const get = vi.fn((_path: string, options?: ApiCallOptions) => {
      if (options?.signal) signals.push(options.signal);
      return new Promise(resolve => { finishOld = resolve; });
    });
    messaging.listMessages.mockImplementation(createMessagingClient({ get } as unknown as ApiMethods).listMessages);
    const { result, unmount } = renderHook(() => useRooms(platform()), { wrapper });
    await drain();
    expect(get).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(result.current.historyLoading).toBe(false);
    expect(result.current.historyErrorMessage).toMatch(/timed out/i);
    expect(get).toHaveBeenCalledTimes(1);
    expect(signals[0].aborted).toBe(true);
    get.mockResolvedValue({ messages: [second], nextAfterSeq: null });
    await act(async () => result.current.loadHistory());
    expect(get).toHaveBeenCalledTimes(2);
    expect(result.current.messages.map(m => m.messageSeq)).toEqual([2]);
    await act(async () => finishOld({ messages: [message], nextAfterSeq: null, nextBeforeSeq: null }));
    expect(result.current.messages.map(m => m.messageSeq)).toEqual([2]);
    unmount();
  });

  it.each(["room", "scope", "unmount"])("cancels history on %s changes without stale writes", async change => {
    const pending: Array<{signal?: AbortSignal; resolve: (page: unknown) => void}> = [];
    const get = vi.fn((_path: string, options?: ApiCallOptions) => new Promise(resolve => pending.push({signal: options?.signal, resolve})));
    messaging.listMessages.mockImplementation(createMessagingClient({ get } as unknown as ApiMethods).listMessages);
    const { result, rerender, unmount } = renderHook(({ userId }) => useRooms(platform({userId})), { wrapper, initialProps: {userId: "user-a"} });
    await drain();
    expect(pending).toHaveLength(1);
    if (change === "unmount") unmount();
    else if (change === "scope") rerender({userId: "user-c"});
    else act(() => result.current.selectRoom("room-2"));
    await drain();
    expect(pending[0].signal?.aborted).toBe(true);
    if (change !== "unmount") {
      const replacement = {...second, roomId: change === "room" ? "room-2" : room.roomId};
      await act(async () => pending[1].resolve({messages: [replacement], nextAfterSeq: null, nextBeforeSeq: null}));
      await act(async () => pending[0].resolve({messages: [message], nextAfterSeq: null, nextBeforeSeq: null}));
      expect(result.current.messages).toEqual([replacement]);
      expect(result.current.historyError).toBe(false);
      unmount();
    }
  });

  it.each(["event", 403, 404] as const)("cancels history when access is removed by %s", async removal => {
    const pending: Array<{ signal?: AbortSignal; resolve: (page: unknown) => void }> = [];
    const get = vi.fn((_path: string, options?: ApiCallOptions) => new Promise(resolve => pending.push({ signal: options?.signal, resolve })));
    messaging.listMessages.mockImplementation(createMessagingClient({ get } as unknown as ApiMethods).listMessages);
    const { result, unmount } = renderHook(() => useRooms(platform()), { wrapper });
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(pending).toHaveLength(1);
    if (removal !== "event") messaging.getRoom.mockRejectedValue(new ApiError(removal, null, "Access removed"));
    await act(async () => dispatch({
      schemaVersion: 1, eventId: "removed", occurredAt: message.createdAt, roomId: room.roomId,
      type: removal === "event" ? "room.access.removed" : "room.metadata.changed",
      payload: { roomId: room.roomId, roomRevision: 2 },
    } as LiveEvent));
    await waitFor(() => expect(result.current.accessRemoved).toBe(true));
    expect(pending[0].signal?.aborted).toBe(true);
    expect(result.current.historyLoading).toBe(false);
    expect(result.current.historyComplete).toBe(false);
    expect(result.current.historyError).toBe(false);
    await act(async () => pending[0].resolve({ messages: [message], nextAfterSeq: null, nextBeforeSeq: null }));
    expect(result.current.messages).toEqual([]);

    if (removal === "event") {
      // Reopening must not restore a cache refilled by that late response.
      act(() => result.current.selectRoom("room-2"));
      await drain();
      act(() => result.current.selectRoom(room.roomId));
      await drain();
      expect(result.current.messages).toEqual([]);
      expect(result.current.historyLoading).toBe(true);
      await act(async () => pending.at(-1)!.resolve({ messages: [second], nextAfterSeq: null, nextBeforeSeq: null }));
      expect(result.current.messages).toEqual([second]);
    }
    unmount();
  });
});
