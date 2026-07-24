/**
 * Realtime status invalidation (REALTIME, spec PR-7).
 *
 * GatewayProvider subscribes ONCE to the backend's `onboarding_status_changed`
 * push (core/services/status_events.py's publish_status_changed) and mutates
 * the shared `/container/status` SWR key. This pins that one subscription:
 * the event reaches every consumer within one round trip, no poll tick, and
 * an unrelated event must not trigger a spurious revalidation.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { GatewayProvider, useGateway } from "@/hooks/useGateway";

vi.mock("@/lib/api", () => ({
  WS_URL: "wss://test.example/ws",
  useApi: () => ({ post: vi.fn(async () => ({})) }),
}));
const mutateMock = vi.fn();
vi.mock("swr", () => ({ mutate: (...args: unknown[]) => mutateMock(...args) }));
vi.mock("@/lib/presence", () => ({ isPresent: () => false, lastInputAgeMs: () => Infinity }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => "token-1" }),
  useUser: () => ({ user: null }),
}));

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  closed = false;

  constructor(_url: string) {
    MockWebSocket.instances.push(this);
  }

  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  receive(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }

  send() {}
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

describe("useGateway realtime status invalidation", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    mutateMock.mockClear();
    vi.stubGlobal("WebSocket", MockWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("mutates /container/status on an onboarding_status_changed push", async () => {
    let ctx!: ReturnType<typeof useGateway>;
    render(
      <GatewayProvider>
        <Probe onCtx={(c) => (ctx = c)} />
      </GatewayProvider>,
    );

    await act(async () => {
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.open();
    });
    expect(ctx.isConnected).toBe(true);

    await act(async () => {
      ws.receive({ type: "event", event: "onboarding_status_changed", payload: {} });
    });

    expect(mutateMock).toHaveBeenCalledWith("/container/status");
  });

  it("does not mutate on an unrelated event", async () => {
    render(
      <GatewayProvider>
        <Probe onCtx={() => {}} />
      </GatewayProvider>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.open();
    });
    mutateMock.mockClear();

    await act(async () => {
      ws.receive({ type: "event", event: "status_change", payload: { state: "HEALTHY" } });
    });

    expect(mutateMock).not.toHaveBeenCalledWith("/container/status");
  });
});
