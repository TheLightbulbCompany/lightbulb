// @vitest-environment jsdom
/**
 * Poll cadence for the two backstop reads behind the health dot.
 *
 * The hook is mounted from WorkspaceSidebar, which lives OUTSIDE
 * ProvisioningStepper and therefore never unmounts for the life of the shell.
 * Its polls used to run at 10s (/container/status) + 5s (health RPC) forever,
 * additively with the stepper's own poll on the same status key, which was the
 * single largest source of idle request traffic on /chat.
 *
 * These tests lock the backoff: slow while everything looks healthy, fast the
 * moment any input suggests otherwise, so a real degradation — and its
 * recovery — is still noticed within seconds.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

import { useSystemHealth } from "../useSystemHealth";

const gateway = {
  isConnected: true,
  reconnectAttempt: 0,
  isParked: false,
  onEvent: () => () => {},
};

// Captured hook arguments so the test can assert the cadence actually passed
// to each data hook, not just the derived state.
let statusOptions: { refreshInterval?: number } | undefined;
let rpcConfig: { refreshInterval?: unknown } | undefined;

// Mutable RPC result so tests can drive the health probe's data/error.
const rpcResult: { data: { ok?: boolean } | undefined; error: Error | undefined } = {
  data: { ok: true },
  error: undefined,
};

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    ...gateway,
    nodeConnected: false,
    error: null,
    send: vi.fn(),
    sendReq: vi.fn().mockResolvedValue(null),
    sendChat: vi.fn(),
    onChatMessage: () => () => {},
    reconnect: vi.fn(),
  }),
}));

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: (options: { refreshInterval?: number }) => {
    statusOptions = options;
    return {
      container: { status: "running", phase: "ready" },
      phase: "normal",
      blocked: null,
      refresh: vi.fn(),
    };
  },
}));

vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (
    _method: string | null,
    _params: unknown,
    config: { refreshInterval?: unknown },
  ) => {
    rpcConfig = config;
    return { data: rpcResult.data, error: rpcResult.error };
  },
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ post: vi.fn().mockResolvedValue(null) }),
}));

/** Resolve the health RPC's refreshInterval, which is passed in function form. */
function healthInterval(latest: { ok?: boolean } | undefined): number {
  const fn = rpcConfig?.refreshInterval as (d: unknown) => number;
  expect(fn).toBeTypeOf("function");
  return fn(latest);
}

describe("useSystemHealth poll cadence", () => {
  beforeEach(() => {
    gateway.isConnected = true;
    gateway.reconnectAttempt = 0;
    gateway.isParked = false;
    rpcResult.data = { ok: true };
    rpcResult.error = undefined;
    statusOptions = undefined;
    rpcConfig = undefined;
  });

  it("polls slowly in the steady state", () => {
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("HEALTHY");
    // 60s + 60s = 2 req/min, down from 6 + 12 = 18 req/min.
    expect(statusOptions?.refreshInterval).toBe(60_000);
    expect(healthInterval({ ok: true })).toBe(60_000);
  });

  it("keeps the slow cadence while deliberately parked", () => {
    // A parked box is asleep on purpose. Treating it as degraded would put
    // every idle trial container into a permanent fast poll — the exact
    // always-on traffic this backoff exists to remove.
    gateway.isParked = true;
    gateway.isConnected = false;
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("PARKED");
    expect(statusOptions?.refreshInterval).toBe(60_000);
  });

  it("drops to the fast cadence when the socket is down and not parked", () => {
    gateway.isConnected = false;
    renderHook(() => useSystemHealth());
    expect(statusOptions?.refreshInterval).toBe(10_000);
  });

  it("drops to the fast cadence while reconnecting", () => {
    gateway.reconnectAttempt = 2;
    renderHook(() => useSystemHealth());
    expect(statusOptions?.refreshInterval).toBe(10_000);
  });

  it("drops to the fast cadence when the health probe reports not-ok", () => {
    rpcResult.data = { ok: false };
    renderHook(() => useSystemHealth());
    expect(statusOptions?.refreshInterval).toBe(10_000);
    expect(healthInterval({ ok: false })).toBe(5_000);
  });

  it("drops to the fast cadence when the health probe REJECTS with a surfaced error", () => {
    // A rejection that ISN'T the "No container" 404-equivalent (a gateway
    // timeout, say) surfaces as healthError. Keying on the error keeps the dot
    // from sitting on HEALTHY until the slow status poll came round.
    rpcResult.data = undefined;
    rpcResult.error = new Error("gateway timeout");
    renderHook(() => useSystemHealth());
    expect(statusOptions?.refreshInterval).toBe(10_000);
  });

  it("drops to the fast cadence when a vanished container is swallowed to undefined", () => {
    // useGatewayRpc's 404-equivalent path catches "No container" rejections and
    // returns undefined data with NO error, so healthError never fires. Keying
    // on `health === undefined` while connected keeps container-death detection
    // at the fast cadence in that case too, instead of letting the dot sit on
    // HEALTHY for up to a full slow-poll interval.
    rpcResult.data = undefined;
    rpcResult.error = undefined;
    renderHook(() => useSystemHealth());
    expect(statusOptions?.refreshInterval).toBe(10_000);
  });

  it("returns to the slow cadence once a degradation clears", () => {
    rpcResult.data = { ok: false };
    const { rerender } = renderHook(() => useSystemHealth());
    expect(statusOptions?.refreshInterval).toBe(10_000);

    rpcResult.data = { ok: true };
    rerender();
    expect(statusOptions?.refreshInterval).toBe(60_000);
  });
});
