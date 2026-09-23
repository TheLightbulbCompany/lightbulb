// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useProvisioningState } from "../useProvisioningState";


const auth = vi.hoisted(() => ({
  isSignedIn: true, orgId: "org_123" as string | undefined,
  uiOrgId: "org_123" as string | undefined,
  getToken: vi.fn(),
}));
const token = (orgId: string) => `header.${btoa(JSON.stringify({ o: { id: orgId } }))}.signature`;
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => auth,
  useOrganization: () => ({ isLoaded: true, organization: auth.uiOrgId ? { id: auth.uiOrgId } : null }),
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
    auth.orgId = "org_123";
    auth.uiOrgId = "org_123";
    auth.getToken.mockReset().mockResolvedValue(token("org_123"));
  });

  afterEach(() => {
    vi.useRealTimers();
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


  describe("workspace JWT barrier", () => {
    it("forces one fresh scope verification then uses cached org tokens for later reads", async () => {
      vi.useFakeTimers();
      const fetchMock = vi.fn(async () => new Response(JSON.stringify({
        runtime_phase: "starting", lifecycle: "starting", chat_available: false,
      }), { status: 200 }));
      global.fetch = fetchMock;
      const { result, rerender } = renderHook(() => useProvisioningState(), { wrapper });
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      // A later read (a live event, focus or Retry) — there is no poll.
      await act(async () => { result.current.refresh(); await vi.advanceTimersByTimeAsync(100); });
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(auth.getToken.mock.calls.filter(([options]) => options.skipCache)).toEqual([
        [{ organizationId: "org_123", skipCache: true }],
      ]);
      expect(auth.getToken).toHaveBeenLastCalledWith({ organizationId: "org_123" });
      auth.orgId = "org_new"; auth.uiOrgId = "org_new";
      auth.getToken.mockResolvedValue(token("org_new"));
      rerender();
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      await act(async () => { result.current.refresh(); await vi.advanceTimersByTimeAsync(100); });
      expect(auth.getToken.mock.calls.filter(([options]) => options.skipCache)).toEqual([
        [{ organizationId: "org_123", skipCache: true }],
        [{ organizationId: "org_new", skipCache: true }],
      ]);
      expect(auth.getToken).toHaveBeenLastCalledWith({ organizationId: "org_new" });
    });
    it("isolates an already-started HTTP response from a new workspace and keeps reading the new one", async () => {
      vi.useFakeTimers();
      let resolveA!: (response: Response) => void;
      let resolveB!: (response: Response) => void;
      const bodyB = { service_name: "workspace-b", runtime_phase: "starting", lifecycle: "starting", chat_available: false };
      const respond = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
      const fetchMock = vi.fn()
        .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveA = resolve; }))
        .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveB = resolve; }))
        .mockImplementation(async () => respond(bodyB));
      global.fetch = fetchMock;
      const { result, rerender } = renderHook(() => useProvisioningState(), { wrapper });
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      auth.orgId = "org_b"; auth.uiOrgId = "org_b";
      auth.getToken.mockResolvedValue(token("org_b"));
      rerender();
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(result.current.container).toBeNull();
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await act(async () => { resolveB(respond(bodyB)); });
      expect(result.current.container?.service_name).toBe("workspace-b");
      await act(async () => { resolveA(respond({ service_name: "workspace-a", runtime_phase: "ready", lifecycle: "ready", chat_available: true })); });
      expect(result.current.container?.service_name).toBe("workspace-b");
      expect(result.current.container?.runtime_phase).toBe("starting");
      const calls = fetchMock.mock.calls.length;
      await act(async () => { result.current.refresh(); await vi.advanceTimersByTimeAsync(100); });
      expect(fetchMock.mock.calls.length).toBeGreaterThan(calls);
      expect(result.current.container?.service_name).toBe("workspace-b");
    });
    function stubFetch() {
      const fetchMock = vi.fn().mockImplementation(async () => new Response(
        JSON.stringify({ container_status: "running", runtime_phase: "ready", lifecycle: "ready" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ));
      global.fetch = fetchMock;
      return fetchMock;
    }
    it.each(["signed out", "no org", "UI mismatch", "missing token", "wrong token", "malformed token", "obsolete token"])("never fetches with %s", async (state) => {
      if (state === "signed out") auth.isSignedIn = false;
      if (state === "no org") { auth.orgId = undefined; auth.uiOrgId = undefined; }
      if (state === "UI mismatch") auth.uiOrgId = "org_other";
      if (state === "missing token") auth.getToken.mockResolvedValue(null);
      if (state === "wrong token") auth.getToken.mockResolvedValue(token("org_other"));
      if (state === "malformed token") auth.getToken.mockResolvedValue("not-a-jwt");
      if (state === "obsolete token") auth.getToken.mockResolvedValue(`header.${btoa(JSON.stringify({ org_id: "org_123" }))}.signature`);
      const fetchMock = stubFetch();
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 60)); });
      expect(fetchMock).not.toHaveBeenCalled();
      expect(result.current.phase).toBe("loading");
    });
    it("uses a freshly minted matching workspace token for the request", async () => {
      const fetchMock = stubFetch();
      renderHook(() => useProvisioningState(), { wrapper });
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      expect(auth.getToken).toHaveBeenCalledWith({ organizationId: "org_123", skipCache: true });
      expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/container/status"), expect.objectContaining({
        headers: expect.objectContaining({ Authorization: `Bearer ${token("org_123")}` }),
      }));
    });
    it("waits for a pending token and discards it if the workspace changes", async () => {
      let resolveToken!: (value: string) => void;
      auth.getToken.mockReturnValueOnce(new Promise<string>((resolve) => { resolveToken = resolve; }));
      const fetchMock = stubFetch();
      const { rerender } = renderHook(() => useProvisioningState(), { wrapper });
      expect(fetchMock).not.toHaveBeenCalled();
      auth.orgId = "org_new"; auth.uiOrgId = "org_new";
      auth.getToken.mockResolvedValue(null);
      rerender();
      await act(async () => { resolveToken(token("org_123")); });
      expect(fetchMock).not.toHaveBeenCalled();
    });
    it("retries token propagation and starts fetching once the token matches", async () => {
      auth.getToken.mockResolvedValueOnce(token("org_other"));
      const fetchMock = stubFetch();
      renderHook(() => useProvisioningState(), { wrapper });
      await waitFor(() => expect(auth.getToken).toHaveBeenCalled());
      expect(fetchMock).not.toHaveBeenCalled();
      await waitFor(() => expect(fetchMock).toHaveBeenCalled(), { timeout: 2500 });
    });
    it("revalidates the token on refresh rather than reusing an expired or wrong-org token", async () => {
      const fetchMock = stubFetch();
      const { result } = renderHook(() => useProvisioningState(), { wrapper });
      await waitFor(() => expect(result.current.phase).toBe("normal"));
      fetchMock.mockClear();
      auth.getToken.mockResolvedValue(token("org_other"));
      await act(async () => { result.current.refresh(); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 60)); });
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });
});
