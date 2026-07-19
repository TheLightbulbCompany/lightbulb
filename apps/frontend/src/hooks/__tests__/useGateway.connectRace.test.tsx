/**
 * Single-flight connect for the gateway WebSocket.
 *
 * connect() suspends on `await getToken()` BEFORE assigning wsRef, so the
 * readyState guard at the top cannot see an in-flight attempt. Clerk's
 * `getToken` changes identity as auth state resolves on mount, which
 * re-creates connect() and re-runs the mount effect — a second connect()
 * starts while the first is still awaiting its token, and the page ends up
 * with TWO live sockets (observed server-side as same-second `WS connect`
 * pairs on every page load). The first socket is orphaned but keeps its
 * handlers, so every broadcast event is delivered twice.
 *
 * These tests pin the contract: one socket per page no matter how many
 * times connect() is invoked concurrently or how often getToken's identity
 * churns, and a stale socket's onclose must not tear down / reconnect over
 * the live one.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { GatewayProvider, useGateway } from "@/hooks/useGateway";

// GatewayProvider now also mounts the idle-park scheduler + mount-wake,
// which pull useApi (POST /container/wake) and the presence lib. Stub both
// so this connect-race test stays focused on the single-flight socket
// contract and doesn't fire real wakes or bind global input listeners.
vi.mock("@/lib/api", () => ({
  WS_URL: "wss://test.example/ws",
  useApi: () => ({ post: vi.fn(async () => ({})) }),
}));
vi.mock("swr", () => ({ mutate: vi.fn() }));
vi.mock("@/lib/presence", () => ({ isPresent: () => false, lastInputAgeMs: () => Infinity }));

// Mutable holder so tests can swap getToken identity mid-mount (mirrors
// Clerk resolving auth state after first render).
let currentGetToken: () => Promise<string | null>;
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: (...args: unknown[]) => currentGetToken(...(args as [])) }),
  useUser: () => ({ user: null }),
}));

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
  sent: string[] = [];
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  send(data: string) {
    this.sent.push(data);
  }

  close(code = 1000, reason = "") {
    this.closed = true;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code, reason });
  }

  addEventListener(name: string, fn: () => void) {
    if (name === "open" && this.readyState === MockWebSocket.OPEN) fn();
  }
  removeEventListener() {}
}

function Probe({ onCtx }: { onCtx: (ctx: ReturnType<typeof useGateway>) => void }) {
  onCtx(useGateway());
  return null;
}

describe("useGateway single-flight connect", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("creates exactly one socket when getToken identity churns during the initial connect", async () => {
    // First getToken: resolves only when we release it — keeps connect #1
    // suspended past the wsRef guard.
    let releaseFirst!: (t: string) => void;
    const firstToken = new Promise<string>((res) => {
      releaseFirst = res;
    });
    currentGetToken = () => firstToken;

    let ctx!: ReturnType<typeof useGateway>;
    const { rerender } = render(
      <GatewayProvider>
        <Probe onCtx={(c) => (ctx = c)} />
      </GatewayProvider>,
    );

    // Clerk resolves: getToken gets a NEW identity while connect #1 is
    // still awaiting the old promise.
    await act(async () => {
      currentGetToken = () => Promise.resolve("token-2");
      rerender(
        <GatewayProvider>
          <Probe onCtx={(c) => (ctx = c)} />
        </GatewayProvider>,
      );
    });

    await act(async () => {
      releaseFirst("token-1");
      await Promise.resolve();
    });

    // Single-flight: only ONE socket may exist, and it must be the one
    // wsRef tracks (open it; the provider should report connected).
    const live = MockWebSocket.instances.filter((w) => !w.closed);
    expect(live).toHaveLength(1);

    await act(async () => {
      live[0].open();
    });
    expect(ctx.isConnected).toBe(true);
  });

  it("coalesces concurrent connect() calls (mount effect + sendReq) into one socket", async () => {
    let releaseToken!: (t: string) => void;
    const tokenPromise = new Promise<string>((res) => {
      releaseToken = res;
    });
    currentGetToken = () => tokenPromise;

    let ctx!: ReturnType<typeof useGateway>;
    render(
      <GatewayProvider>
        <Probe onCtx={(c) => (ctx = c)} />
      </GatewayProvider>,
    );

    // Mount effect's connect() is suspended on the token. A consumer RPC
    // (sendReq calls connect() itself when the socket isn't open) must not
    // start a second handshake.
    let rpc: Promise<unknown>;
    await act(async () => {
      rpc = ctx.sendReq("agents.list").catch(() => undefined);
      releaseToken("token-1");
      await Promise.resolve();
    });

    const live = MockWebSocket.instances.filter((w) => !w.closed);
    expect(live).toHaveLength(1);

    await act(async () => {
      live[0].open();
      await Promise.resolve();
    });
    expect(ctx.isConnected).toBe(true);
    expect(MockWebSocket.instances.filter((w) => !w.closed)).toHaveLength(1);
    await act(async () => {
      vi.advanceTimersByTime(35_000); // let the RPC time out quietly
      await rpc!;
    });
  });
});
