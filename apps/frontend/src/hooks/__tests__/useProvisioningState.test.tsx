// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useProvisioningState } from "../useProvisioningState";
import { SIGNUP_INTENT_COOKIE } from "@/lib/signupIntent";

// Per-test Clerk auth state. `orgId` is the JWT's org claim — the signal the
// team-intent poll gate reads (see the hook's `teamIntentPending` comment).
const auth = vi.hoisted(() => ({
  isSignedIn: true as boolean,
  orgId: undefined as string | undefined,
}));

// Clerk's useAuth — return a stub.
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: auth.isSignedIn,
    orgId: auth.orgId,
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
    auth.isSignedIn = true;
    auth.orgId = undefined;
    // jsdom persists document.cookie across tests in a file.
    document.cookie = `${SIGNUP_INTENT_COOKIE}=; max-age=0; path=/`;
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

  /**
   * Team-signup poll gate.
   *
   * `GET /container/status` dispatches `ensure_user_ready` server-side, so
   * this poll IS the provisioning trigger — not a read. Both chat shells call
   * this hook (transitively, via `useGetStartedDerived`) ABOVE their
   * `onboardingPhase !== "ready"` early return, and React runs hooks before
   * early returns, so a team signup that lands on /chat fires the poll during
   * the renders before `useOnboardingGate`'s redirect to /onboarding lands —
   * provisioning a throwaway PERSONAL container, trial_local row and
   * Paperclip company for a user who is about to create an org.
   *
   * Suppressing the poll (rather than steering the post-signup URL) is what
   * makes the fix whole: it holds no matter which URL Clerk chooses.
   */
  describe("team-signup intent gate", () => {
    function stubOkFetch() {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            container_status: "running",
            runtime_phase: "ready",
            lifecycle: "ready",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
      global.fetch = fetchMock;
      return fetchMock;
    }

    it("does not poll while team intent is pending (cookie set, no org yet)", async () => {
      const fetchMock = stubOkFetch();
      document.cookie = `${SIGNUP_INTENT_COOKIE}=team; path=/`;
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      // Same shape as the `enabled: false` case above — give SWR a tick to
      // potentially fire, then assert it never did.
      await new Promise((r) => setTimeout(r, 50));
      expect(fetchMock).not.toHaveBeenCalled();
      expect(result.current.phase).toBe("loading");
    });

    it("polls once the org exists even with the cookie still set — a stale cookie must never dead-poll an org owner", async () => {
      const fetchMock = stubOkFetch();
      document.cookie = `${SIGNUP_INTENT_COOKIE}=team; path=/`;
      auth.orgId = "org_123";
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      await waitFor(() => expect(result.current.phase).toBe("normal"));
      expect(fetchMock).toHaveBeenCalled();
    });

    it("polls immediately when there is no intent cookie — personal signups are unchanged", async () => {
      const fetchMock = stubOkFetch();
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      await waitFor(() => expect(result.current.phase).toBe("normal"));
      expect(fetchMock).toHaveBeenCalled();
    });

    it("resumes polling on the next render once the cookie is cleared", async () => {
      const fetchMock = stubOkFetch();
      document.cookie = `${SIGNUP_INTENT_COOKIE}=team; path=/`;
      const { result, rerender } = renderHook(() => useProvisioningState(), { wrapper });
      await new Promise((r) => setTimeout(r, 50));
      expect(fetchMock).not.toHaveBeenCalled();

      // `useOnboardingGate` clears the cookie when it forwards the user to
      // /onboarding?intent=team. The cookie read is not reactive, so this
      // pins the self-heal: the very next render picks the change up.
      document.cookie = `${SIGNUP_INTENT_COOKIE}=; max-age=0; path=/`;
      rerender();
      await waitFor(() => expect(result.current.phase).toBe("normal"));
      expect(fetchMock).toHaveBeenCalled();
    });

    it("fails OPEN: an undecodable cookie value polls rather than stranding the user", async () => {
      const fetchMock = stubOkFetch();
      // A bare "%" is what `decodeURIComponent` throws a URIError on — the
      // read happens during render, so an unguarded throw here would take the
      // whole chat shell down AND leave the user unable to provision.
      document.cookie = `${SIGNUP_INTENT_COOKIE}=%; path=/`;
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      await waitFor(() => expect(result.current.phase).toBe("normal"));
      expect(fetchMock).toHaveBeenCalled();
    });
  });
});
