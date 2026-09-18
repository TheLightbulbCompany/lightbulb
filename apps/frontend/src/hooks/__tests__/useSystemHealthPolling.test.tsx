// @vitest-environment jsdom
/**
 * The health dot dials NO runtime RPC. The runtime parks an idle microVM
 * invisibly and wakes it on ANY invoke, so a periodic `health` probe would hold
 * every workspace awake — and billed — forever for a dot whose answer
 * `runtime_phase` already gives unconditionally. The backend composer refuses
 * the same call for the same reason (`readiness_state.compute_status`); this is
 * the client half, and it is a COST regression guard, not a rendering one.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

import { useSystemHealth } from "../useSystemHealth";

const live = { liveConnected: true };

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({ ...live, nodeConnected: false }),
}));

let container: Record<string, unknown> | null = { container_status: "running", lifecycle: "ready" };

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: () => ({ container, phase: "normal", blocked: null, refresh: vi.fn() }),
}));

const useRuntimeRpc = vi.fn(() => ({ data: undefined, error: undefined }));
vi.mock("@/hooks/useRuntimeRpc", () => ({ useRuntimeRpc: (...a: unknown[]) => useRuntimeRpc(...(a as [])) }));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ post: vi.fn().mockResolvedValue(null) }),
}));

describe("useSystemHealth", () => {
  beforeEach(() => {
    live.liveConnected = true;
    useRuntimeRpc.mockClear();
    container = { container_status: "running", lifecycle: "ready" };
  });

  it("dials no runtime RPC for a running workspace", () => {
    const { result } = renderHook(() => useSystemHealth());
    expect(useRuntimeRpc).not.toHaveBeenCalled();
    // No probe is not "down": a running row is reported ready unconditionally.
    expect(result.current.state).toBe("HEALTHY");
  });

  it("dials none for a parked one either", () => {
    container = { container_status: "stopped", lifecycle: "parked" };
    renderHook(() => useSystemHealth());
    expect(useRuntimeRpc).not.toHaveBeenCalled();
  });

  it("dials none before a status body has landed", () => {
    container = null;
    renderHook(() => useSystemHealth());
    expect(useRuntimeRpc).not.toHaveBeenCalled();
  });
});
