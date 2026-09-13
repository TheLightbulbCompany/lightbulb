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
    expect(first.subscriptions().map((subscription) => subscription.channel)).toEqual([userChannel, roomA]);
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
});
