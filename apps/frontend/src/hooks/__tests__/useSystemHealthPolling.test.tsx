// @vitest-environment jsdom
/**
 * Poll cadence for the gateway health RPC backstop read behind the health
 * dot.
 *
 * `/container/status` itself is no longer polled per-consumer (spec PR-9,
 * CLIENT-HOOK) — `useProvisioningState` now delegates to the ONE shared
 * `useOnboardingStatus` bounded loop (see `packages/shared/src/hooks/
 * useOnboardingStatus.ts`), so this hook no longer owns its own status-poll
 * cadence (the deleted `STATUS_POLL_STEADY`/`STATUS_POLL_DEGRADED` +
 * `looksDegraded` machinery this file used to lock). What remains is the
 * health RPC's own backoff: slow while everything looks healthy, fast the
 * moment the probe reports degraded, so a real degradation — and its
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

// Captured hook config so the test can assert the cadence actually passed
// to the health RPC data hook, not just the derived state.
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
  useProvisioningState: () => ({
    container: { container_status: "running", lifecycle: "ready" },
    phase: "normal",
    blocked: null,
    refresh: vi.fn(),
  }),
}));

vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (
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

describe("useSystemHealth health-RPC poll cadence", () => {
  beforeEach(() => {
    gateway.isConnected = true;
    gateway.reconnectAttempt = 0;
    gateway.isParked = false;
    rpcResult.data = { ok: true };
    rpcResult.error = undefined;
    rpcConfig = undefined;
  });

  it("polls slowly while the probe reports healthy", () => {
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("HEALTHY");
    expect(healthInterval({ ok: true })).toBe(60_000);
  });

  it("polls fast once the probe reports not-ok", () => {
    rpcResult.data = { ok: false };
    renderHook(() => useSystemHealth());
    expect(healthInterval({ ok: false })).toBe(5_000);
  });
});
