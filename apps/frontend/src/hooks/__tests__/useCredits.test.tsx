import { beforeEach, expect, describe, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useCredits } from "../useCredits";

let orgId: string | null = "org_1";
let userId: string | null = "user_1";
let isLoaded = true;
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    orgId,
    userId,
    isLoaded,
    isSignedIn: Boolean(userId),
    orgRole: "org:admin",
    getToken: vi.fn().mockResolvedValue("test-token"),
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) =>
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;

describe("useCredits", () => {
  beforeEach(() => {
    orgId = "org_1";
    userId = "user_1";
    isLoaded = true;
    localStorage.clear();
  });

  it("persists a failed workspace top-up intent across a remount and reuses it", async () => {
    let topUpCalls = 0;
    const fetchMock = vi.fn((url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(new Response(
      String(url).includes("top_up")
        ? (++topUpCalls === 1
          ? JSON.stringify({ detail: "billing unavailable" })
          : JSON.stringify({ status: "requested", amount_cents: 2000 }))
        : JSON.stringify({ balance_microcents: 0, balance_dollars: "0.00" }),
      { status: String(url).includes("top_up") && topUpCalls === 1 ? 502 : 200 },
    )));
    global.fetch = fetchMock;
    orgId = "org_credits";
    userId = "user_credits";
    const firstMount = renderHook(() => useCredits(), { wrapper });

    await act(async () => {
      await expect(firstMount.result.current.startTopUp(2000)).rejects.toThrow("API 502");
    });
    const stored = JSON.parse(localStorage.getItem("lightbulb:credits-top-up:user_credits:org_credits") ?? "{}");
    expect(stored).toMatchObject({
      user_id: "user_credits",
      workspace_id: "org_credits",
      amount_cents: 2000,
    });
    firstMount.unmount();

    const secondMount = renderHook(() => useCredits(), { wrapper });
    await act(async () => { await secondMount.result.current.startTopUp(5000); });

    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes("top_up"));
    const first = JSON.parse(String(calls[0]?.[1]?.body));
    const second = JSON.parse(String(calls[1]?.[1]?.body));
    expect(first).toMatchObject({ amount_cents: 2000, request_id: expect.any(String) });
    expect(second).toEqual(first);
    expect(JSON.parse(localStorage.getItem("lightbulb:credits-top-up:user_credits:org_credits") ?? "{}")).toMatchObject({
      request_id: first.request_id,
      amount_cents: 2000,
      state: "requested",
    });
  });

  it("keeps an accepted-but-uncollected top-up locked across a reload", async () => {
    const fetchMock = vi.fn((url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(new Response(
      String(url).includes("top_up")
        ? JSON.stringify({ status: "requested", amount_cents: 2000 })
        : JSON.stringify({ balance_microcents: 0, balance_dollars: "0.00" }),
      { status: 200 },
    )));
    global.fetch = fetchMock;
    orgId = "org_accepted";
    userId = "user_accepted";
    const firstMount = renderHook(() => useCredits(), { wrapper });

    await act(async () => { await firstMount.result.current.startTopUp(2000); });
    const first = JSON.parse(String(fetchMock.mock.calls.find(([url]) => String(url).includes("top_up"))?.[1]?.body));
    firstMount.unmount();

    const secondMount = renderHook(() => useCredits(), { wrapper });
    expect(secondMount.result.current.pendingTopUp).toMatchObject({
      user_id: "user_accepted",
      workspace_id: "org_accepted",
      request_id: first.request_id,
      amount_cents: 2000,
      state: "requested",
    });
    await act(async () => { await secondMount.result.current.startTopUp(5000); });
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes("top_up"));
    expect(JSON.parse(String(calls[1]?.[1]?.body))).toEqual(first);
  });

  it("requests balance reconciliation for the browser's persisted UUID", async () => {
    const requestId = "7e22fb41-07e3-4f9d-920c-68ce9a7710b7";
    localStorage.setItem(
      "lightbulb:credits-top-up:user_1:org_1",
      JSON.stringify({
        user_id: "user_1",
        workspace_id: "org_1",
        request_id: requestId,
        amount_cents: 2000,
        state: "requested",
      }),
    );
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ balance_microcents: 0, balance_dollars: "0.00" })),
    );
    global.fetch = fetchMock;

    renderHook(() => useCredits(), { wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(String(fetchMock.mock.calls[0][0])).toContain(
      `/billing/credits/balance?request_id=${requestId}`,
    );
  });

  it("opens the existing billing portal for payment recovery", async () => {
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { href: "" } as Location,
    });
    const fetchMock = vi.fn((url: RequestInfo | URL) => Promise.resolve(new Response(
      String(url).includes("/billing/portal")
        ? JSON.stringify({ portal_url: "https://billing.stripe.test/session" })
        : JSON.stringify({ balance_microcents: 0, balance_dollars: "0.00" }),
      { status: 200 },
    )));
    global.fetch = fetchMock;

    try {
      const mounted = renderHook(() => useCredits(), { wrapper });
      await act(async () => { await mounted.result.current.openBilling(); });
      expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/billing/portal"))).toBe(true);
      expect(window.location.href).toBe("https://billing.stripe.test/session");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: original,
      });
    }
  });

  it.each(["paid", "failed"] as const)("unlocks a matching %s payment so a new top-up gets a new UUID", async (status) => {
    let balanceCalls = 0;
    const fetchMock = vi.fn((url: RequestInfo | URL, _init?: RequestInit) => {
      if (String(url).includes("top_up")) return Promise.resolve(new Response(JSON.stringify({ status: "requested", amount_cents: 2000 }), { status: 200 }));
      balanceCalls += 1;
      const requestId = JSON.parse(localStorage.getItem("lightbulb:credits-top-up:user_terminal:org_terminal") ?? "{}");
      return Promise.resolve(new Response(JSON.stringify({
        balance_microcents: 0,
        balance_dollars: "0.00",
        active_top_up: balanceCalls > 1 ? { request_id: requestId.request_id, status } : null,
      }), { status: 200 }));
    });
    global.fetch = fetchMock;
    orgId = "org_terminal";
    userId = "user_terminal";
    const mounted = renderHook(() => useCredits(), { wrapper });
    await act(async () => { await mounted.result.current.startTopUp(2000); });
    const oldId = mounted.result.current.pendingTopUp?.request_id;
    await act(async () => { await mounted.result.current.refresh(); });

    expect(mounted.result.current.pendingTopUp).toBeNull();
    expect(mounted.result.current.topUpOutcome).toBe(status);
    await act(async () => { await mounted.result.current.startTopUp(2000); });
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes("top_up"));
    expect(JSON.parse(String(calls[1]?.[1]?.body)).request_id).not.toBe(oldId);
  });

  it.each(["paid", "failed"] as const)("ignores a late %s reconciliation after switching accounts", async (status) => {
    const firstKey = "lightbulb:credits-top-up:user_1:org_1";
    localStorage.setItem(firstKey, JSON.stringify({ user_id: userId, workspace_id: orgId, request_id: "request_A", amount_cents: 2000, state: "pending" }));
    let finishA!: (response: Response) => void;
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { finishA = resolve; }))
      .mockResolvedValue(new Response(JSON.stringify({ balance_microcents: 0, balance_dollars: "0.00" })));
    global.fetch = fetchMock;
    const mounted = renderHook(() => useCredits(), { wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    userId = "user_B";
    orgId = "org_B";
    mounted.rerender();
    await act(async () => {
      finishA(new Response(JSON.stringify({ balance_microcents: 0, balance_dollars: "0.00", active_top_up: { request_id: "request_A", status } })));
    });
    expect(mounted.result.current.topUpOutcome).toBeNull();
    expect(mounted.result.current.pendingTopUp).toBeNull();
    expect(localStorage.getItem(firstKey)).not.toBeNull();
  });

  it("clears a prior outcome when the workspace changes", async () => {
    localStorage.setItem("lightbulb:credits-top-up:user_1:org_1", JSON.stringify({ user_id: userId, workspace_id: orgId, request_id: "request_A", amount_cents: 2000, state: "requested" }));
    global.fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({
      balance_microcents: 0, balance_dollars: "0.00", active_top_up: { request_id: "request_A", status: "failed" },
    }))));
    const mounted = renderHook(() => useCredits(), { wrapper });
    await waitFor(() => expect(mounted.result.current.topUpOutcome).toBe("failed"));
    orgId = "org_B";
    mounted.rerender();
    expect(mounted.result.current.topUpOutcome).toBeNull();
  });

  it("does not read a workspace balance until Clerk user and workspace auth are ready", () => {
    orgId = null;
    const fetchMock = vi.fn();
    global.fetch = fetchMock;
    renderHook(() => useCredits(), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();
    orgId = "org_1";
    userId = null;
    renderHook(() => useCredits(), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();
    userId = "user_1";
    isLoaded = false;
    renderHook(() => useCredits(), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();
    orgId = "org_1";
  });
});
