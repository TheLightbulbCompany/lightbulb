// @vitest-environment jsdom
/**
 * Parked-state health derivation — the exact if/else branch (checked BEFORE
 * the stopped/!isConnected branches) that regressed twice in review. A parked
 * trial box is ECS-stopped and socket-closed, so without this branch it would
 * derive CONTAINER_DOWN / GATEWAY_DOWN_DEAD (red dot + Restart CTA) over a
 * chat surface the stepper simultaneously renders as ready. These tests lock
 * both the client-park (isParked) and backend-park (lifecycle="parked") paths
 * so a future reorder of that chain can't ship silently.
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
let container: Record<string, unknown> = { container_status: "running", lifecycle: "ready" };

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
  useProvisioningState: () => ({ container, phase: "normal", blocked: null, refresh: vi.fn() }),
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: { ok: true }, error: null }),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn().mockResolvedValue(null) }) }));

describe("useSystemHealth parked", () => {
  beforeEach(() => {
    gateway.isConnected = true;
    gateway.isParked = false;
    container = { container_status: "running", lifecycle: "ready" };
  });

  it("client-parked (isParked) with socket closed maps to PARKED, not GATEWAY_DOWN_DEAD", () => {
    // The ~3min window after park() before the backend reflects it: socket
    // closed, container row still says running.
    gateway.isParked = true;
    gateway.isConnected = false;
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("PARKED");
    expect(result.current.canRecover).toBe(false);
  });

  it("backend-parked (lifecycle=parked, container_status=stopped) maps to PARKED, not CONTAINER_DOWN", () => {
    gateway.isParked = false;
    gateway.isConnected = false;
    container = { container_status: "stopped", lifecycle: "parked" };
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("PARKED");
    expect(result.current.canRecover).toBe(false);
  });

  it("a genuinely stopped (non-parked) box still maps to CONTAINER_DOWN", () => {
    gateway.isConnected = false;
    container = { container_status: "stopped", lifecycle: "provisioning" };
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("CONTAINER_DOWN");
    expect(result.current.canRecover).toBe(true);
  });
});
