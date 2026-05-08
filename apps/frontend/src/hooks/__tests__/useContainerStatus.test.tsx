import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useContainerStatus } from "../useContainerStatus";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
);

describe("useContainerStatus", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns null container when /container/status returns 402 (provision gate up)", async () => {
    // The stepper's useProvisioningState renders the gate UI; non-stepper
    // consumers (OverviewPanel, HealthIndicator, etc.) just need
    // "no container info to render". 202 and 402 both collapse to null
    // for those callers.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ detail: { blocked: { code: "subscribe_required" } } }),
        { status: 402, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useContainerStatus(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.container).toBeNull();
    expect(result.current.error).toBeUndefined();
  });

  it("returns null container when /container/status returns 202 (team workspace pending)", async () => {
    // Backend Task 8: org members whose admin-owned workspace is missing
    // get 202 Accepted instead of 404. useProvisioningState renders the
    // "waiting on your admin" state in the stepper; non-stepper consumers
    // just collapse to null here.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "team workspace pending" }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useContainerStatus(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.container).toBeNull();
    expect(result.current.error).toBeUndefined();
  });

  it("returns the container row when /container/status returns 200", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          service_name: "openclaw-user-abc",
          status: "running",
          substatus: "gateway_healthy",
          created_at: "2026-05-04T00:00:00Z",
          updated_at: "2026-05-04T01:00:00Z",
          region: "us-east-1",
          last_error: null,
          last_error_at: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useContainerStatus(), { wrapper });
    await waitFor(() => expect(result.current.container).not.toBeNull());
    expect(result.current.container?.status).toBe("running");
  });
});
