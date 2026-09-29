import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  APPSYNC_EVENT_WS_PROTOCOL,
  AppSyncEventsClient,
  base64Url,
  buildSubprotocols,
  framesChannelFor,
  reconnectDelayMs,
  type LiveStatus,
} from "./appsyncEvents";
import type { LiveEvent } from "./types";

class FakeSocket {
  static instances: FakeSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = FakeSocket.CONNECTING;
  sent: Record<string, unknown>[] = [];
  closes: Array<{ code?: number; reason?: string }> = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string, public protocols: string[]) {
    FakeSocket.instances.push(this);
  }
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  receive(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  close(code?: number, reason?: string) {
    if (this.readyState === FakeSocket.CLOSED) return;
    this.readyState = FakeSocket.CLOSED;
    this.closes.push({ code, reason });
    this.onclose?.();
  }
  /** Server-side drop. */
  drop() {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }
  ack(timeoutMs = 60_000) {
    this.receive({ type: "connection_ack", connectionTimeoutMs: timeoutMs });
  }
  sentOf(type: string) {
    return this.sent.filter((m) => m.type === type);
  }
  ackSubscribe(channel: string) {
    const sub = this.sentOf("subscribe").find((m) => m.channel === channel);
    if (!sub) throw new Error(`no subscribe sent for ${channel}`);
    this.receive({ type: "subscribe_success", id: sub.id });
    return sub.id as string;
  }
}

const HTTP = "https://abc123.appsync-api.us-east-1.amazonaws.com/event";
const RT = "wss://abc123.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime";
const WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const USER = `/ws/${WS}/users/11111111-1111-4111-8111-111111111111`;
const ROOM = `/ws/${WS}/rooms/22222222-2222-4222-8222-222222222222/33333333-3333-4333-8333-333333333333`;
const FRAMES = framesChannelFor(ROOM);
const OTHER_ROOM = `/ws/${WS}/rooms/44444444-4444-4444-8444-444444444444/55555555-5555-4555-8555-555555555555`;
const FOREIGN_FRAMES = framesChannelFor(OTHER_ROOM);

function liveEvent(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    schemaVersion: 1,
    eventId: "evt-1",
    type: "room.metadata.changed",
    occurredAt: "2026-09-09T00:00:00Z",
    roomId: "room-1",
    roomRevision: 3,
    payload: { roomId: "room-1", roomRevision: 3 },
    ...overrides,
  };
}

function setup(opts: { token?: () => Promise<string | null>; random?: () => number } = {}) {
  const events: LiveEvent[] = [];
  const statuses: LiveStatus[] = [];
  const errors: Array<[string, string | undefined]> = [];
  let n = 0;
  const getToken = vi.fn(opts.token ?? (async () => `tok-${++n}`));
  const client = new AppSyncEventsClient({
    httpEndpoint: HTTP,
    realtimeEndpoint: RT,
    getToken,
    onEvent: (e) => events.push(e),
    onStatus: (s) => statuses.push(s),
    onError: (kind, channel) => errors.push([kind, channel]),
    uuid: () => `sub-${++n}`,
    random: opts.random ?? (() => 0.5),
  });
  return { client, events, statuses, errors, getToken };
}

async function flush() {
  await vi.advanceTimersByTimeAsync(0);
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

describe("subprotocol encoding", () => {
  it("base64url-encodes {Authorization, host} without padding and adds the event protocol", () => {
    const [header, proto] = buildSubprotocols(HTTP, "jwt.token.value");
    expect(proto).toBe(APPSYNC_EVENT_WS_PROTOCOL);
    expect(header.startsWith("header-")).toBe(true);
    const encoded = header.slice("header-".length);
    expect(encoded).not.toMatch(/[+/=]/);
    const decoded = JSON.parse(
      Buffer.from(encoded.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"),
    );
    expect(decoded).toEqual({
      Authorization: "jwt.token.value",
      host: "abc123.appsync-api.us-east-1.amazonaws.com",
    });
  });

  it("round-trips non-ASCII through UTF-8", () => {
    expect(Buffer.from(base64Url("héllo ✓"), "base64url").toString("utf8")).toBe("héllo ✓");
  });
});

describe("connection handshake", () => {
  it("fetches a fresh token per connection, opens with both subprotocols, and sends connection_init", async () => {
    const { client, getToken } = setup();
    await flush();
    expect(getToken).toHaveBeenCalledTimes(1);
    const ws = FakeSocket.instances[0];
    expect(ws.url).toBe(RT);
    expect(ws.protocols[1]).toBe(APPSYNC_EVENT_WS_PROTOCOL);
    expect(ws.protocols[0]).toContain("header-");
    ws.open();
    expect(ws.sent).toEqual([{ type: "connection_init" }]);
    client.close();
  });

  it("accepts no application data and sends no subscribe before connection_ack", async () => {
    const { client, events, statuses } = setup();
    client.setChannels([USER]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.receive({ type: "data", id: "anything", event: JSON.stringify(liveEvent()) });
    expect(events).toEqual([]);
    expect(ws.sentOf("subscribe")).toEqual([]);
    expect(statuses.every((s) => !s.connected)).toBe(true);
    ws.ack();
    await flush();
    expect(statuses.at(-1)?.connected).toBe(true);
    expect(ws.sentOf("subscribe")).toHaveLength(1);
    client.close();
  });

  it("retries with backoff when no token is available and never opens a socket", async () => {
    const { client, errors } = setup({ token: async () => null });
    await flush();
    expect(FakeSocket.instances).toHaveLength(0);
    expect(errors).toEqual([["no_token", undefined]]);
    await vi.advanceTimersByTimeAsync(500);
    expect(errors).toHaveLength(2);
    client.close();
  });
});

describe("subscriptions", () => {
  it("subscribes with a unique id and a fresh Authorization per channel, live only after subscribe_success", async () => {
    const { client, statuses, getToken } = setup();
    client.setChannels([USER, ROOM]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const subs = ws.sentOf("subscribe");
    expect(subs.map((s) => s.channel)).toEqual([USER, ROOM]);
    expect(new Set(subs.map((s) => s.id)).size).toBe(2);
    // Connection token was tok-1; each subscribe fetched its own.
    expect(getToken).toHaveBeenCalledTimes(3);
    expect(subs.map((s) => s.authorization)).toEqual([
      { Authorization: "tok-3" },
      { Authorization: "tok-5" },
    ]);
    expect(statuses.at(-1)?.liveChannels).toEqual([]);
    ws.ackSubscribe(USER);
    expect(statuses.at(-1)?.liveChannels).toEqual([USER]);
    ws.ackSubscribe(ROOM);
    expect(statuses.at(-1)?.liveChannels).toEqual([USER, ROOM]);
    client.close();
  });

  it("drops application data until its subscription is acknowledged", async () => {
    const { client, events } = setup();
    client.setChannels([ROOM]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const id = ws.sentOf("subscribe")[0].id;
    ws.receive({ type: "data", id, event: JSON.stringify(liveEvent()) });
    expect(events).toEqual([]);
    ws.receive({ type: "subscribe_success", id });
    ws.receive({ type: "data", id, event: JSON.stringify(liveEvent()) });
    expect(events).toHaveLength(1);
    client.close();
  });

  it("keeps exactly one room subscription: replacing the room unsubscribes the old id and drops its late frames", async () => {
    const { client, events } = setup();
    client.setChannels([USER, ROOM]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const oldId = ws.ackSubscribe(ROOM);
    const nextRoom = `/ws/${WS}/rooms/44444444-4444-4444-8444-444444444444/55555555-5555-4555-8555-555555555555`;
    client.setChannels([USER, nextRoom]);
    await flush();
    expect(ws.sentOf("unsubscribe")).toEqual([{ type: "unsubscribe", id: oldId }]);
    expect(ws.sentOf("subscribe").map((s) => s.channel)).toEqual([USER, ROOM, nextRoom]);
    // A frame on the replaced subscription id is stale — never delivered.
    ws.receive({ type: "data", id: oldId, event: JSON.stringify(liveEvent()) });
    expect(events).toEqual([]);
    ws.ackSubscribe(nextRoom);
    const newId = ws.sentOf("subscribe").find((s) => s.channel === nextRoom)!.id;
    ws.receive({ type: "data", id: newId, event: JSON.stringify(liveEvent()) });
    expect(events).toHaveLength(1);
    client.close();
  });

  it("surfaces subscribe_error as a redacted kind + channel and drops the subscription", async () => {
    const { client, errors, statuses } = setup();
    client.setChannels([ROOM]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const id = ws.sentOf("subscribe")[0].id;
    ws.receive({ type: "subscribe_error", id, errors: [{ message: "secret-server-detail", errorType: "Unauthorized" }] });
    expect(errors).toEqual([["subscribe_error", ROOM]]);
    expect(JSON.stringify(errors)).not.toContain("secret-server-detail");
    expect(statuses.at(-1)?.liveChannels).toEqual([]);
    client.close();
  });

  it("a rejected subscription closes the socket and resubscribes on the reconnect", async () => {
    // Prod 2026-09-13: one transient SubscribeClientError left the socket acked
    // with no live channel, so nothing ever resubscribed and the tab showed
    // "Reconnecting live updates" until reload.
    const { client, statuses } = setup();
    client.setChannels([ROOM]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    ws.receive({ type: "subscribe_error", id: ws.sentOf("subscribe")[0].id });
    expect(ws.closes).toEqual([{ code: 4002, reason: "subscribe rejected" }]);
    expect(statuses.at(-1)?.connected).toBe(false);
    await vi.advanceTimersByTimeAsync(500);
    const second = FakeSocket.instances[1];
    expect(second).toBeDefined();
    second.open();
    second.ack();
    await flush();
    expect(second.sentOf("subscribe").map((s) => s.channel)).toEqual([ROOM]);
    second.ackSubscribe(ROOM);
    expect(statuses.at(-1)?.liveChannels).toEqual([ROOM]);
    client.close();
  });

  it("backs off when the rejection repeats, and resets once the channel goes live", async () => {
    const { client } = setup({ random: () => 1 - Number.EPSILON }); // worst-case delay
    client.setChannels([ROOM]);
    await flush();
    const reject = (ws: FakeSocket) => ws.receive({ type: "subscribe_error", id: ws.sentOf("subscribe")[0].id });
    const cycle = async (ws: FakeSocket) => {
      ws.open();
      ws.ack();
      await flush();
      return ws;
    };
    reject(await cycle(FakeSocket.instances[0])); // attempt 0 → [0, 500ms)
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(2);
    reject(await cycle(FakeSocket.instances[1])); // attempt 1 → [0, 1000ms): an ACK must not reset it
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(3);
    // A fully live cycle is what proves the connection works — that resets it.
    const third = await cycle(FakeSocket.instances[2]);
    third.ackSubscribe(ROOM);
    third.drop();
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(4);
    client.close();
  });

  it("closes every subscription on sign-out / close and ignores frames afterwards", async () => {
    const { client, events } = setup();
    client.setChannels([USER]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const id = ws.ackSubscribe(USER);
    client.close();
    expect(ws.sentOf("unsubscribe")).toEqual([{ type: "unsubscribe", id }]);
    expect(ws.closes).toHaveLength(1);
    ws.receive({ type: "data", id, event: JSON.stringify(liveEvent()) });
    expect(events).toEqual([]);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});

describe("frames channel", () => {
  it("framesChannelFor swaps the room segment for frames, same workspace/room/deliveryKey", () => {
    expect(FRAMES).toBe(`/ws/${WS}/frames/22222222-2222-4222-8222-222222222222/33333333-3333-4333-8333-333333333333`);
  });

  it("subscribes both the room and its /frames sibling", async () => {
    const { client, errors } = setup();
    client.setChannels([USER, ROOM, FRAMES]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    expect(ws.sentOf("subscribe").map((s) => s.channel)).toEqual([USER, ROOM, FRAMES]);
    expect(errors).toEqual([]);
    ws.ackSubscribe(USER);
    ws.ackSubscribe(ROOM);
    ws.ackSubscribe(FRAMES);
    expect(errors).toEqual([]);
    client.close();
  });

  it("refuses a /frames channel that does not match the current room as protocol_error, and never subscribes it", async () => {
    const { client, errors } = setup();
    client.setChannels([ROOM, FOREIGN_FRAMES]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    expect(errors).toEqual([["protocol_error", undefined]]);
    expect(ws.sentOf("subscribe").map((s) => s.channel)).toEqual([ROOM]);
    client.close();
  });

  it("refuses a /frames channel with no room at all", async () => {
    const { client, errors } = setup();
    client.setChannels([USER, FRAMES]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    expect(errors).toEqual([["protocol_error", undefined]]);
    expect(ws.sentOf("subscribe").map((s) => s.channel)).toEqual([USER]);
    client.close();
  });

  it("removing the frames channel unsubscribes only it, leaving the room live", async () => {
    const { client } = setup();
    client.setChannels([ROOM, FRAMES]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const roomId = ws.ackSubscribe(ROOM);
    const framesId = ws.ackSubscribe(FRAMES);
    client.setChannels([ROOM]);
    expect(ws.sentOf("unsubscribe")).toEqual([{ type: "unsubscribe", id: framesId }]);
    expect(ws.sentOf("subscribe").map((s) => s.channel)).toEqual([ROOM, FRAMES]);
    // The room subscription itself was never touched.
    expect(roomId).not.toBe(framesId);
    client.close();
  });
});

describe("data delivery", () => {
  async function liveRoom() {
    const ctx = setup();
    ctx.client.setChannels([ROOM]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const id = ws.ackSubscribe(ROOM);
    return { ...ctx, ws, id };
  }

  it("delivers the single stringified event of a real AppSync data frame", async () => {
    // Observed on dev 2026-09-10: `{"type":"data","id":…,"event":"<json>"}` — a
    // string, not an array. Rejecting it dropped every live room event.
    const { client, events, ws, id } = await liveRoom();
    ws.receive({ type: "data", id, event: JSON.stringify(liveEvent({ eventId: "solo" })) });
    expect(events.map((e) => e.eventId)).toEqual(["solo"]);
    client.close();
  });

  it("parses every stringified item of a data event array and dedupes by eventId", async () => {
    const { client, events, ws, id } = await liveRoom();
    ws.receive({
      type: "data",
      id,
      event: [
        JSON.stringify(liveEvent({ eventId: "a" })),
        JSON.stringify(liveEvent({ eventId: "b" })),
        JSON.stringify(liveEvent({ eventId: "a" })),
      ],
    });
    ws.receive({ type: "data", id, event: [JSON.stringify(liveEvent({ eventId: "b" }))] });
    expect(events.map((e) => e.eventId)).toEqual(["a", "b"]);
    client.close();
  });

  it("drops malformed, unknown-type, and payload-invalid events without throwing", async () => {
    const { client, events, ws, id } = await liveRoom();
    ws.receive({ type: "data", id, event: "{not json" });
    ws.receive({ type: "data", id, event: [JSON.stringify(liveEvent({ type: "room.deleted" }))] });
    ws.receive({ type: "data", id, event: [JSON.stringify(liveEvent({ payload: { roomId: 1 } }))] });
    ws.receive({ type: "data", id, event: [JSON.stringify(liveEvent({ schemaVersion: 2 }))] });
    ws.receive({
      type: "data",
      id,
      event: [JSON.stringify(
        liveEvent({ type: "room.run.frame", payload: { roomId: "r", agentId: "a", runId: "x", frame: { type: "nope" } } }),
      )],
    });
    expect(events).toEqual([]);
    ws.receive({
      type: "data",
      id,
      event: [JSON.stringify(
        liveEvent({
          type: "room.run.frame",
          payload: {
            roomId: "room-1",
            agentId: "a",
            runId: "x",
            threadRootMessageId: "thread-1",
            frame: { type: "chunk", content: "hi", runId: "x" },
          },
        }),
      )],
    });
    expect(events).toHaveLength(1);
    expect(events[0]?.payload).toMatchObject({ threadRootMessageId: "thread-1" });
    client.close();
  });

  it("a subscriber exception does not break delivery of the next event", async () => {
    const seen: string[] = [];
    let n = 0;
    const client = new AppSyncEventsClient({
      httpEndpoint: HTTP,
      realtimeEndpoint: RT,
      getToken: async () => "tok",
      onEvent: (e) => {
        seen.push(e.eventId);
        if (e.eventId === "boom") throw new Error("subscriber bug");
      },
      uuid: () => `id-${++n}`,
    });
    client.setChannels([ROOM]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack();
    await flush();
    const id = ws.ackSubscribe(ROOM);
    ws.receive({ type: "data", id, event: [JSON.stringify(liveEvent({ eventId: "boom" }))] });
    ws.receive({ type: "data", id, event: [JSON.stringify(liveEvent({ eventId: "after" }))] });
    expect(seen).toEqual(["boom", "after"]);
    client.close();
  });
});

describe("keepalive and reconnect", () => {
  it("closes when connectionTimeoutMs elapses without ka, and every ka resets the watchdog", async () => {
    const { client, statuses } = setup();
    client.setChannels([USER]);
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack(10_000);
    await flush();
    await vi.advanceTimersByTimeAsync(9_000);
    ws.receive({ type: "ka" });
    await vi.advanceTimersByTimeAsync(9_000);
    expect(ws.closes).toEqual([]);
    await vi.advanceTimersByTimeAsync(1_500);
    expect(ws.closes).toHaveLength(1);
    expect(statuses.at(-1)?.connected).toBe(false);
    client.close();
  });

  it("reconnects with full-jitter backoff (500ms→30s cap), a fresh token, resubscribes, and resets once live", async () => {
    const random = vi.fn(() => 1 - Number.EPSILON); // worst-case delay each attempt
    const { client, getToken, statuses } = setup({ random });
    client.setChannels([USER]);
    await flush();
    const first = FakeSocket.instances[0];
    first.open();
    first.ack();
    await flush();
    first.ackSubscribe(USER);
    expect(getToken).toHaveBeenCalledTimes(2);

    first.drop(); // attempt 0 → [0, 500ms)
    await vi.advanceTimersByTimeAsync(498);
    expect(FakeSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.instances).toHaveLength(2);
    const second = FakeSocket.instances[1];
    expect(second.protocols[0]).not.toBe(first.protocols[0]); // fresh token → new header
    second.drop(); // attempt 1 → [0, 1000ms)
    await vi.advanceTimersByTimeAsync(998);
    expect(FakeSocket.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.instances).toHaveLength(3);
    // Drive the ceiling: attempts 2..7 grow 2s,4s,8s,16s,30s(cap),30s.
    for (let i = 0; i < 6; i++) {
      const ws = FakeSocket.instances.at(-1)!;
      ws.drop();
      await vi.advanceTimersByTimeAsync(30_000);
    }
    const settled = FakeSocket.instances.at(-1)!;
    settled.open();
    settled.ack();
    await flush();
    // Resubscribed on the new socket with a fresh token, then reset: the next
    // drop waits < 500ms again.
    expect(settled.sentOf("subscribe").map((s) => s.channel)).toEqual([USER]);
    expect(statuses.at(-1)?.ackCount).toBe(2);
    settled.ackSubscribe(USER);
    settled.drop();
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances.length).toBeGreaterThan(9);
    client.close();
  });

  it("the delay is jittered uniformly below the ceiling", () => {
    expect(reconnectDelayMs(0, () => 0)).toBe(0);
    expect(reconnectDelayMs(0, () => 0.5)).toBe(250);
    expect(reconnectDelayMs(10, () => 0.999)).toBeLessThan(30_000);
  });

  it("rejects frames and continuations from a superseded socket generation", async () => {
    let release!: (t: string) => void;
    const tokens: Array<Promise<string>> = [];
    const { client, events } = setup({
      token: () => {
        const p = new Promise<string>((res) => {
          release = res;
        });
        tokens.push(p);
        return p;
      },
    });
    client.setChannels([USER]);
    release("tok-a");
    await flush();
    const first = FakeSocket.instances[0];
    first.open();
    first.ack();
    await flush();
    // The subscribe's token fetch is now pending on the first socket.
    first.drop();
    await vi.advanceTimersByTimeAsync(500);
    release("tok-late"); // resolves the stale subscribe fetch AND/OR the reconnect
    await flush();
    expect(first.sentOf("subscribe")).toEqual([]); // stale continuation never sent
    // A late frame on the dead socket is ignored.
    first.receive({ type: "data", id: "x", event: JSON.stringify(liveEvent()) });
    expect(events).toEqual([]);
    client.close();
  });

  it("closes an un-acked socket on connection_error and reconnects", async () => {
    const { client, errors } = setup();
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.receive({ type: "connection_error", errors: [{ message: "bad token" }] });
    expect(errors).toEqual([["connection_error", undefined]]);
    expect(ws.closes).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(2);
    client.close();
  });

  it("never sends a client ping", async () => {
    const { client } = setup();
    await flush();
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.ack(5_000);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(ws.sent.map((m) => m.type)).toEqual(["connection_init"]);
    client.close();
  });
});
