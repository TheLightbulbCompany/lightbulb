// @vitest-environment jsdom
/**
 * Parked-state health derivation — the exact if/else branch (checked BEFORE
 * the stopped/live-reconnect branches) that regressed twice in review. A parked
 * trial box reads as stopped and live delivery is disconnected, so without this
 * branch it would derive CONTAINER_DOWN (red dot + Restart CTA) over a chat
 * surface the stepper simultaneously renders as ready. These tests lock the
 * backend lifecycle contract so a future reorder can't ship silently.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

import { useSystemHealth } from "../useSystemHealth";

const live = {
  liveConnected: true,
};
let container: Record<string, unknown> = { container_status: "running", lifecycle: "ready" };

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    ...live,
    nodeConnected: false,
  }),
}));
vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: () => ({ container, phase: "normal", blocked: null, refresh: vi.fn() }),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn().mockResolvedValue(null) }) }));

describe("useSystemHealth parked", () => {
  beforeEach(() => {
    live.liveConnected = true;
    container = { container_status: "running", lifecycle: "ready" };
  });

  it("backend-parked (lifecycle=parked, container_status=stopped) maps to PARKED, not CONTAINER_DOWN", () => {
    live.liveConnected = false;
    container = { container_status: "stopped", lifecycle: "parked" };
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("PARKED");
    expect(result.current.canRecover).toBe(false);
  });

  it("a provisioning container takes precedence over parked metadata", () => {
    live.liveConnected = false;
    container = {
      container_status: "provisioning",
      container_substatus: "auto_retry",
      lifecycle: "provisioning",
      parked: true,
    };
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("STARTING");
    expect(result.current.reason).toBe("Restarting agents...");
  });

  it("a genuinely stopped (non-parked) box still maps to CONTAINER_DOWN", () => {
    live.liveConnected = false;
    container = { container_status: "stopped", lifecycle: "provisioning" };
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("CONTAINER_DOWN");
    expect(result.current.canRecover).toBe(true);
  });
});
