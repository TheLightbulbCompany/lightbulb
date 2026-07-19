// @vitest-environment jsdom
/**
 * Idle-park mechanics of the shared gateway connection machine:
 * park() releases the socket and refuses reconnection, unpark() reconnects,
 * and a park() racing an in-flight getToken() never leaves a live socket
 * behind (the round-3 review race).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useGatewayConnection } from "@isol8/shared";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSING = 2;
  static CLOSED = 3;
  url: string;
  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  closes: Array<{ code: number; reason: string }> = [];
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }
  send() {}
  close(code = 1000, reason = "") {
    this.readyState = MockWebSocket.CLOSED;
    this.closes.push({ code, reason });
    this.onclose?.({ code, reason });
  }
}

const realWS = globalThis.WebSocket;

beforeEach(() => {
  MockWebSocket.instances = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).WebSocket = MockWebSocket;
});
afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).WebSocket = realWS;
});

const opts = (getToken = async () => "tok") => ({
  getToken,
  wsUrl: "wss://test.example/ws",
  uuid: () => "id-1",
});

describe("gateway idle park", () => {
  it("park() closes the socket cleanly and refuses reconnection", async () => {
    const { result } = renderHook(() => useGatewayConnection(opts()));
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(1));
    await act(async () => {
      MockWebSocket.instances[0].open();
    });
    expect(result.current.isConnected).toBe(true);

    await act(async () => {
      result.current.park();
    });
    expect(result.current.isParked).toBe(true);
    expect(result.current.isConnected).toBe(false);
    expect(MockWebSocket.instances[0].closes.at(-1)).toEqual({ code: 1000, reason: "Idle park" });

    // A reconnect attempt while parked is a no-op — no new socket.
    await act(async () => {
      result.current.reconnect();
    });
    expect(MockWebSocket.instances.length).toBe(1);
  });

  it("unpark() clears the flag and reconnects", async () => {
    const { result } = renderHook(() => useGatewayConnection(opts()));
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(1));
    await act(async () => {
      result.current.park();
    });
    await act(async () => {
      result.current.unpark();
    });
    expect(result.current.isParked).toBe(false);
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(2));
  });

  it("park() during an in-flight getToken() leaves no live socket (race)", async () => {
    let release!: (t: string) => void;
    const gate = new Promise<string>((r) => {
      release = r;
    });
    const { result } = renderHook(() => useGatewayConnection(opts(() => gate)));
    // connect() is suspended on the token await; no socket yet.
    await act(async () => {
      result.current.park();
    });
    // Now let the token resolve — connect() resumes and must NOT open a live
    // socket, or must immediately close one it constructs.
    await act(async () => {
      release("tok");
      await gate.catch(() => {});
    });
    const live = MockWebSocket.instances.filter(
      (w) => w.readyState !== MockWebSocket.CLOSED,
    );
    expect(live.length).toBe(0);
    expect(result.current.isConnected).toBe(false);
    expect(result.current.isParked).toBe(true);
  });
});
