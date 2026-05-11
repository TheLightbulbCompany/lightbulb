// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

import { useSystemHealth } from "../useSystemHealth";

// onEvent registers a status_change subscriber; the test grabs the handler
// out of the ref so it can fire backend-shaped push events directly.
let capturedOnEventHandler:
  | ((eventName: string, data: unknown) => void)
  | null = null;

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send: vi.fn(),
    sendReq: vi.fn().mockResolvedValue(null),
    sendChat: vi.fn(),
    onEvent: (handler: (eventName: string, data: unknown) => void) => {
      capturedOnEventHandler = handler;
      return () => {
        capturedOnEventHandler = null;
      };
    },
    onChatMessage: () => () => {},
    reconnect: vi.fn(),
  }),
}));

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: () => ({
    container: { status: "running", phase: "ready" },
    phase: "normal",
    blocked: null,
    refresh: vi.fn(),
  }),
}));

vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: { ok: true }, error: null }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ post: vi.fn().mockResolvedValue(null) }),
}));

describe("useSystemHealth status_change mapping", () => {
  beforeEach(() => {
    capturedOnEventHandler = null;
  });

  it("maps backend GATEWAY_DOWN_LLM_BUSY -> frontend GATEWAY_BUSY (yellow, no recover)", async () => {
    const { result } = renderHook(() => useSystemHealth());

    // Baseline: nothing pushed yet, derived path lands on HEALTHY (mocked
    // container.status=running + isConnected=true + health.ok=true).
    expect(result.current.state).toBe("HEALTHY");

    // Fire the backend's parked-teardown push.
    act(() => {
      capturedOnEventHandler?.("status_change", {
        state: "GATEWAY_DOWN_LLM_BUSY",
        reason: "Agent is still working — keeping the run alive",
      });
    });

    await waitFor(() => {
      expect(result.current.state).toBe("GATEWAY_BUSY");
    });
    // The "agent thinking" state is intentionally NOT surfaced as
    // recoverable — we don't want a Restart CTA mid-turn.
    expect(result.current.canRecover).toBe(false);
    expect(result.current.actionLabel).toBeNull();
  });

  it("maps backend GATEWAY_DOWN_DEAD -> frontend GATEWAY_DOWN_DEAD (red, Restart Gateway)", async () => {
    const { result } = renderHook(() => useSystemHealth());

    act(() => {
      capturedOnEventHandler?.("status_change", {
        state: "GATEWAY_DOWN_DEAD",
        reason: "Gateway connection lost",
      });
    });

    await waitFor(() => {
      expect(result.current.state).toBe("GATEWAY_DOWN_DEAD");
    });
    expect(result.current.canRecover).toBe(true);
    expect(result.current.actionLabel).toBe("Restart Gateway");
  });

  it("unknown backend states fall back to HEALTHY (forwards-compat)", async () => {
    const { result } = renderHook(() => useSystemHealth());

    act(() => {
      capturedOnEventHandler?.("status_change", {
        state: "SOME_NEW_BACKEND_STATE",
        reason: "future",
      });
    });

    // Should NOT flip red on a state we don't recognize — better to under-
    // alarm than to scare the user with a false alert if the backend ships
    // a new status code before the frontend has caught up.
    await waitFor(() => {
      expect(result.current.state).toBe("HEALTHY");
    });
  });
});
