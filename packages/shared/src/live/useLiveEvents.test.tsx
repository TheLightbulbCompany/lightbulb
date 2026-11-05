// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLiveEvents, type UseLiveEventsOptions } from "./useLiveEvents";
import type { LiveConfig } from "../messaging/types";

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

const WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const config: LiveConfig = {
  schemaVersion: 1,
  httpEndpoint: "https://events.appsync-api.us-east-1.amazonaws.com/event",
  realtimeEndpoint: "wss://events.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime",
  region: "us-east-1",
  userChannelKey: "11111111-1111-4111-8111-111111111111",
  workspaceId: WS,
};
const userChannel = `/ws/${WS}/users/${config.userChannelKey}`;
const sessionsChannel = `/ws/${WS}/sessions`;
const roomA = `/ws/${WS}/rooms/22222222-2222-4222-8222-222222222222/33333333-3333-4333-8333-333333333333`;
const roomB = `/ws/${WS}/rooms/44444444-4444-4444-8444-444444444444/55555555-5555-4555-8555-555555555555`;

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

function acknowledgeSubscriptions(socket: FakeSocket) {
  for (const subscription of socket.subscriptions()) {
    socket.receive({ type: "subscribe_success", id: subscription.id });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useLiveEvents", () => {
  it("turns over one user and one room subscription and demuxes room frames", async () => {
    const onEvent = vi.fn();
    const onChatMessage = vi.fn();
    let uuid = 0;
    const options: UseLiveEventsOptions = {
      config,
      getToken: vi.fn(async () => "template-token"),
      userChannel,
      roomChannel: roomA as string | null,
      uuid: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, "0")}`,
      onEvent,
      onChatMessage,
    };
    const { result, rerender } = renderHook((props) => useLiveEvents(props), {
      initialProps: options,
    });

    await flush();
    const first = FakeSocket.instances[0];
    act(() => {
      first.open();
      first.ack();
    });
    await flush();
    expect(first.subscriptions().map((subscription) => subscription.channel)).toEqual([userChannel, sessionsChannel, roomA]);
    // Nothing acked yet: not connected, and the room is pending.
    expect(result.current.liveConnected).toBe(false);
    expect(result.current.roomSubscriptionPending).toBe(true);

    // The user channel acking is what "connected" means. The room's own
    // subscribe_success may still be in flight — that is NOT a disconnect.
    const userSubscription = first.subscriptions().find((subscription) => subscription.channel === userChannel)!;
    act(() => first.receive({ type: "subscribe_success", id: userSubscription.id }));
    expect(result.current.liveConnected).toBe(true);
    expect(result.current.roomSubscriptionPending).toBe(true);

    act(() => acknowledgeSubscriptions(first));
    expect(result.current.liveConnected).toBe(true);
    expect(result.current.roomSubscriptionPending).toBe(false);

    const roomSubscription = first.subscriptions().find((subscription) => subscription.channel === roomA)!;
    const event = {
      schemaVersion: 1,
      eventId: "event-1",
      type: "room.run.frame",
      occurredAt: "2026-09-10T00:00:00Z",
      roomId: "22222222-2222-4222-8222-222222222222",
      payload: {
        roomId: "22222222-2222-4222-8222-222222222222",
        agentId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        runId: "run-1",
        frame: { type: "chunk", content: "hello", runId: "run-1" },
      },
    };
    act(() => first.receive({ type: "data", id: roomSubscription.id, event: [JSON.stringify(event)] }));
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ eventId: "event-1" }));
    expect(onChatMessage).toHaveBeenCalledWith(event.payload.frame, expect.objectContaining({ roomId: event.roomId }));

    rerender({ ...options, roomChannel: roomB });
    await flush();
    expect(first.sent).toContainEqual({ type: "unsubscribe", id: roomSubscription.id });
    const roomBSubscription = first.subscriptions().find((subscription) => subscription.channel === roomB)!;
    // Switching rooms re-enters the pending window without ever "disconnecting".
    expect(result.current.liveConnected).toBe(true);
    expect(result.current.roomSubscriptionPending).toBe(true);
    act(() => first.receive({ type: "subscribe_success", id: roomBSubscription.id }));
    expect(result.current.roomSubscriptionPending).toBe(false);

    rerender({ ...options, roomChannel: null });
    expect(first.sent).toContainEqual({ type: "unsubscribe", id: roomBSubscription.id });
    expect(result.current.liveConnected).toBe(true);

    rerender({ ...options, config: null, userChannel: null, roomChannel: null });
    expect(result.current.liveConnected).toBe(false);
    expect(first.readyState).toBe(FakeSocket.CLOSED);
  });

  it("closes everything on workspace.access.removed for this workspace and asks for a fresh live-config", async () => {
    const onEvent = vi.fn();
    const onConfigStale = vi.fn();
    let uuid = 0;
    const { result } = renderHook(() => useLiveEvents({
      config,
      getToken: vi.fn(async () => "template-token"),
      userChannel,
      roomChannel: roomA,
      uuid: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, "0")}`,
      onEvent,
      onConfigStale,
    }));
    await flush();
    const socket = FakeSocket.instances[0];
    act(() => {
      socket.open();
      socket.ack();
    });
    await flush();
    act(() => acknowledgeSubscriptions(socket));
    expect(result.current.liveConnected).toBe(true);

    const user = socket.subscriptions().find((s) => s.channel === userChannel)!;
    const notice = (eventId: string, workspaceId: string) => ({
      schemaVersion: 1,
      eventId,
      type: "workspace.access.removed",
      occurredAt: "2026-10-08T00:00:00Z",
      payload: { workspaceId },
    });
    // Another workspace's notice (a multi-org account) changes nothing here.
    act(() => socket.receive({ type: "data", id: user.id, event: [JSON.stringify(notice("e-1", "ffffffff-ffff-4fff-8fff-ffffffffffff"))] }));
    expect(socket.readyState).toBe(FakeSocket.OPEN);

    act(() => socket.receive({ type: "data", id: user.id, event: [JSON.stringify(notice("e-2", WS))] }));
    await flush();
    expect(socket.readyState).toBe(FakeSocket.CLOSED);
    expect(result.current.liveConnected).toBe(false);
    expect(onConfigStale).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ type: "workspace.access.removed" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(FakeSocket.instances).toHaveLength(1); // no reconnect into the workspace it left
  });

  it("asks for a fresh live-config when the sessions channel is refused or the socket reconnects", async () => {
    const onConfigStale = vi.fn();
    let uuid = 0;
    renderHook(() => useLiveEvents({
      config,
      getToken: vi.fn(async () => "template-token"),
      userChannel,
      roomChannel: null,
      uuid: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, "0")}`,
      onEvent: vi.fn(),
      onConfigStale,
    }));
    await flush();
    const socket = FakeSocket.instances[0];
    act(() => {
      socket.open();
      socket.ack();
    });
    await flush();
    expect(onConfigStale).not.toHaveBeenCalled(); // the first connect is not stale

    // No longer a member: AppSync refuses the sessions subscription.
    const sessions = socket.subscriptions().find((s) => s.channel === sessionsChannel)!;
    act(() => socket.receive({ type: "subscribe_error", id: sessions.id }));
    expect(onConfigStale).toHaveBeenCalledTimes(1);

    // The client reconnects after the refusal; that second ack is stale too.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    const next = FakeSocket.instances[FakeSocket.instances.length - 1];
    expect(next).not.toBe(socket);
    act(() => {
      next.open();
      next.ack();
    });
    await flush();
    expect(onConfigStale).toHaveBeenCalledTimes(2);
  });
});

describe("useLiveEvents tasks channel (super-agent)", () => {
  async function channelsFor(tasksChannel: boolean | undefined) {
    FakeSocket.instances = [];
    renderHook(() => useLiveEvents({
      config, getToken: vi.fn(async () => "t"), userChannel, roomChannel: null, tasksChannel,
      uuid: () => `u-${Math.random()}`, onEvent: vi.fn(),
    }));
    await flush();
    const socket = FakeSocket.instances[0];
    act(() => { socket.open(); socket.ack(); });
    await flush();
    return socket.subscriptions().map((s) => s.channel);
  }

  it("subscribes /ws/<ws>/tasks only when on", async () => {
    expect(await channelsFor(true)).toEqual([userChannel, sessionsChannel, `/ws/${WS}/tasks`]);
  });

  it("no tasks channel while off", async () => {
    expect(await channelsFor(false)).toEqual([userChannel, sessionsChannel]);
    expect(await channelsFor(undefined)).toEqual([userChannel, sessionsChannel]);
  });
});
