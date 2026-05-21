import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useProvisioningState } from "../useProvisioningState";

// Clerk's useAuth — return a stub.
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  // Fresh SWR cache per test to avoid cross-test pollution.
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
);

describe("useProvisioningState", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns phase=normal when /status returns 200 with a container", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "running",
          substatus: "gateway_healthy",
          service_name: "x",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("normal"));
    expect(result.current.container?.status).toBe("running");
    expect(result.current.blocked).toBeNull();
  });

  it("returns phase=blocked when /status returns 402 with blocked payload", async () => {
    // Real wire shape: FastAPI's HTTPException(detail=gate.to_payload())
    // serializes to {"detail": {"blocked": {...}}}. The parser must read
    // body.detail.blocked, not body.blocked.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            blocked: {
              code: "credits_required",
              title: "Top up Claude credits",
              message: "Top up some Claude credits to start your Bedrock container.",
              action: {
                kind: "link",
                label: "Top up now",
                href: "/settings/billing#credits",
                admin_only: false,
              },
              owner_role: "admin",
            },
          },
        }),
        { status: 402, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("blocked"));
    expect(result.current.blocked?.code).toBe("credits_required");
    expect(result.current.blocked?.action.href).toBe("/settings/billing#credits");
    expect(result.current.refreshInterval).toBe(5000);
  });

  it("also accepts a top-level blocked payload (defensive fallback)", async () => {
    // Older mocks / non-FastAPI proxies may put blocked at the top
    // level; the parser should still accept it for resilience.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          blocked: {
            code: "credits_required",
            title: "",
            message: "",
            action: { kind: "link", label: "", href: "", admin_only: false },
            owner_role: "admin",
          },
        }),
        { status: 402, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("blocked"));
    expect(result.current.blocked?.code).toBe("credits_required");
  });

  it("returns phase=needs-signup when /status returns 404 (admin owner has never started signup)", async () => {
    // Backend signal for "owner has never started signup": gate would
    // fire `signup_required` for an admin, but /status converts that
    // to 404 so the chat surface renders the ProviderPicker (the only
    // checkout entry point) instead of trapping the admin in a
    // Subscribe-loop UI. Inactive-row admins still get 402.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "No container found" }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("needs-signup"));
    expect(result.current.container).toBeNull();
    expect(result.current.blocked).toBeNull();
    // Polls on same cadence as blocked/team-pending so a user
    // returning from Stripe before the subscription webhook lands
    // doesn't get stuck on a stale picker.
    expect(result.current.refreshInterval).toBe(5_000);
  });

  it("returns phase=team-pending when /status returns 202 (org member, admin-owned workspace not yet provisioned)", async () => {
    // Backend Task 8: org members whose admin-owned workspace is missing
    // get 202 Accepted with {"detail": "..."}. The hook surfaces this as
    // `phase === "team-pending"` so the stepper can render an
    // admin-waiting empty state and keep polling (5s) for transition to
    // `normal` once the admin's container appears.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "team workspace pending" }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("team-pending"));
    expect(result.current.container).toBeNull();
    expect(result.current.blocked).toBeNull();
    // Members poll fast so the UI flips to `normal` shortly after their
    // admin's container becomes ready.
    expect(result.current.refreshInterval).toBe(5_000);
  });

  it("drops team-pending polling cadence to 30s after the first minute", async () => {
    // Without a slow phase, a member whose admin never provisions would
    // hammer /container/status indefinitely (720 req/hour). The cadence
    // should match `blocked`: 5s for the first minute, then 30s.
    vi.useFakeTimers();
    try {
      global.fetch = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ detail: "team workspace pending" }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      );
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      await vi.waitFor(() => expect(result.current.phase).toBe("team-pending"));
      expect(result.current.refreshInterval).toBe(5_000);

      // Advance past the first 60s window — cadence should drop to 30s.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(61_000);
      });
      await vi.waitFor(() => expect(result.current.refreshInterval).toBe(30_000));
    } finally {
      vi.useRealTimers();
    }
  });

  it("polls every 5s for the first minute while blocked", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            blocked: {
              code: "credits_required",
              title: "",
              message: "",
              action: { kind: "link", label: "", href: "", admin_only: false },
              owner_role: "admin",
            },
          },
        }),
        { status: 402, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("blocked"));
    expect(result.current.refreshInterval).toBe(5000);
  });

  it("refresh() while blocked restarts the 60s fast-poll window", async () => {
    // Clicking "Check again" must restart the 60s 5s-poll window. An
    // `[isBlocked]`-only timer effect would not re-fire on subsequent
    // generations, leaving the cadence stuck at 5s indefinitely after a
    // refresh past the first minute. Here we verify that immediately
    // after `refresh()`, the cadence is still 5s (within the new
    // generation's 60s window).
    vi.useFakeTimers();
    try {
      global.fetch = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              blocked: {
                code: "credits_required",
                title: "",
                message: "",
                action: { kind: "link", label: "", href: "", admin_only: false },
                owner_role: "admin",
              },
            },
          }),
          { status: 402, headers: { "Content-Type": "application/json" } },
        ),
      );
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      await vi.waitFor(() => expect(result.current.phase).toBe("blocked"));
      expect(result.current.refreshInterval).toBe(5000);

      // Advance past the first 60s window — cadence should drop to 30s.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(61_000);
      });
      await vi.waitFor(() => expect(result.current.refreshInterval).toBe(30_000));

      // Manual refresh should bring us back to 5s for another 60s.
      await act(async () => {
        result.current.refresh();
      });
      await vi.waitFor(() => expect(result.current.refreshInterval).toBe(5_000));

      // And after another 60s, cadence drops back to 30s — confirming
      // the timer effect actually re-fired for the new generation.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(61_000);
      });
      await vi.waitFor(() => expect(result.current.refreshInterval).toBe(30_000));
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses consumer-supplied refreshInterval when not in a pending state", async () => {
    // 200 = normal state. The hook's gate-driven cadence is 0 here, so
    // the consumer's refreshInterval should determine polling.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "running",
          substatus: "gateway_healthy",
          service_name: "x",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(
      () => useProvisioningState({ refreshInterval: 10_000 }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.phase).toBe("normal"));
    expect(result.current.refreshInterval).toBe(10_000);
  });

  it("gate-driven cadence wins over consumer refreshInterval when blocked", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            blocked: {
              code: "oauth_required",
              title: "Sign in with ChatGPT",
              message: "x",
              action: { kind: "link", label: "x", href: "x", admin_only: false },
              owner_role: "admin",
            },
          },
        }),
        { status: 402, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(
      () => useProvisioningState({ refreshInterval: 60_000 }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.phase).toBe("blocked"));
    // 5s (pending fast-poll) wins over the consumer's 60s.
    expect(result.current.refreshInterval).toBe(5_000);
  });

  it("enabled: false halts the fetch", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;
    renderHook(() => useProvisioningState({ enabled: false }), { wrapper });
    // Give SWR a tick to potentially fire — assert it never did.
    await new Promise((r) => setTimeout(r, 50));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exposes the SWR error on non-2xx, non-handled statuses", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "boom" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.error).toBeDefined());
    expect(result.current.phase).toBe("loading");
  });
});
