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

const live = {
  liveConnected: true,
};

// Captured hook config so the test can assert the cadence actually passed
// to the health RPC data hook, not just the derived state.
let rpcConfig: { refreshInterval?: unknown } | undefined;

// Mutable RPC result so tests can drive the health probe's data/error.
const rpcResult: { data: { ok?: boolean } | undefined; error: Error | undefined } = {
  data: { ok: true },
  error: undefined,
};

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    ...live,
    nodeConnected: false,
  }),
}));

let container: Record<string, unknown> | null = { container_status: "running", lifecycle: "ready" };

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: () => ({
    container,
    phase: "normal",
    blocked: null,
    refresh: vi.fn(),
  }),
}));

let rpcMethod: string | null | undefined;

vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (
    method: string | null,
    _params: unknown,
    config: { refreshInterval?: unknown },
  ) => {
    rpcMethod = method;
    rpcConfig = config;
    // A null method nulls the SWR key, and SWR never fetches (or holds data) for a null key —
    // so the mock must answer `undefined` there, exactly as the real hook does.
    if (method === null) return { data: undefined, error: undefined };
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
    live.liveConnected = true;
    rpcResult.data = { ok: true };
    rpcResult.error = undefined;
    rpcConfig = undefined;
    rpcMethod = undefined;
    container = { container_status: "running", lifecycle: "ready" };
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

/**
 * M6 task 10, row 6. AgentCore parks an idle microVM invisibly and wakes it on ANY invoke, so
 * a 60s `health` RPC holds a migrated workspace awake (and billed) forever. The backend
 * composer already refuses to probe such a row for the same reason
 * (`readiness_state.compute_status`) — this is the client half.
 */
describe("useSystemHealth on an AgentCore workspace", () => {
  beforeEach(() => {
    live.liveConnected = true;
    rpcResult.data = { ok: true };
    rpcResult.error = undefined;
    rpcMethod = undefined;
  });

  it("never dials the health RPC", () => {
    container = { container_status: "running", lifecycle: "ready", runtime_host: "agentcore" };
    const { result } = renderHook(() => useSystemHealth());
    expect(rpcMethod).toBeNull();
    // Readiness reports an AgentCore row ready unconditionally, so no probe is not "down".
    expect(result.current.state).toBe("HEALTHY");
  });

  it("never dials it for a parked one either", () => {
    container = { container_status: "stopped", lifecycle: "parked", runtime_host: "agentcore" };
    renderHook(() => useSystemHealth());
    expect(rpcMethod).toBeNull();
  });

  it("still polls an ECS row, and an explicit ecs host changes nothing", () => {
    container = { container_status: "running", lifecycle: "ready", runtime_host: "ecs" };
    renderHook(() => useSystemHealth());
    expect(rpcMethod).toBe("health");
  });

  it("holds the probe until the status body lands, so an unknown host cannot wake a microVM", () => {
    container = null;
    renderHook(() => useSystemHealth());
    expect(rpcMethod).toBeNull();
  });
});
