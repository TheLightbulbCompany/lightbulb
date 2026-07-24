// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
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

/**
 * `useProvisioningState` is now a PURE READER over the single shared
 * `useOnboardingStatus` hook (spec PR-9, CLIENT-HOOK) — its own SWR
 * instance, two-phase pending-cadence state machine, and consumer-supplied
 * `refreshInterval`/`revalidateOnFocus` knobs are gone. These tests cover
 * only the classification -> `ProvisioningPhase` mapping this hook still
 * owns; the shared cadence/backstop-loop behavior itself is covered by
 * `packages/shared/src/hooks/useOnboardingStatus.test.ts`.
 */
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
          container_status: "running",
          container_substatus: "gateway_healthy",
          service_name: "x",
          runtime_phase: "ready",
          lifecycle: "ready",
          chat_available: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("normal"));
    expect(result.current.container?.container_status).toBe("running");
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
  });

  it("also accepts a top-level blocked payload (defensive fallback)", async () => {
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
  });

  it("returns phase=team-pending when /status returns 202 (org member, admin-owned workspace not yet provisioned)", async () => {
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
  });

  it("enabled: false halts the fetch", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;
    renderHook(() => useProvisioningState({ enabled: false }), { wrapper });
    // Give SWR a tick to potentially fire — assert it never did.
    await new Promise((r) => setTimeout(r, 50));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exposes a real error on an unrecognized (e.g. 500) status instead of a silently stuck UI", async () => {
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

  it("refresh() triggers an immediate revalidation", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ container_status: "running", runtime_phase: "ready", lifecycle: "ready" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    global.fetch = fetchSpy;
    const { result } = renderHook(() => useProvisioningState(), { wrapper });
    await waitFor(() => expect(result.current.phase).toBe("normal"));
    const before = fetchSpy.mock.calls.length;
    result.current.refresh();
    await waitFor(() => expect(fetchSpy.mock.calls.length).toBeGreaterThan(before));
  });
});
