import { beforeEach, expect, describe, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
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
    const fetchMock = vi.fn((url: RequestInfo | URL) => Promise.resolve(new Response(
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
    const first = JSON.parse(calls[0][1].body);
    const second = JSON.parse(calls[1][1].body);
    expect(first).toMatchObject({ amount_cents: 2000, request_id: expect.any(String) });
    expect(second).toEqual(first);
    expect(JSON.parse(localStorage.getItem("lightbulb:credits-top-up:user_credits:org_credits") ?? "{}")).toMatchObject({
      request_id: first.request_id,
      amount_cents: 2000,
      state: "requested",
    });
  });

  it("keeps an accepted-but-uncollected top-up locked across a reload", async () => {
    const fetchMock = vi.fn((url: RequestInfo | URL) => Promise.resolve(new Response(
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
    const first = JSON.parse(fetchMock.mock.calls.find(([url]) => String(url).includes("top_up"))?.[1].body);
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
    expect(JSON.parse(calls[1][1].body)).toEqual(first);
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
