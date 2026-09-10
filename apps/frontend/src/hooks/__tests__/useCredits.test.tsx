import { expect, describe, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useCredits } from "../useCredits";

let orgId = "org_1";
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ orgId, getToken: vi.fn().mockResolvedValue("test-token") }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) =>
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;

describe("useCredits", () => {
  it("scopes the balance cache to Clerk's active workspace and pins its UUID across retries", async () => {
    const fetchMock = vi.fn((url: RequestInfo | URL) => Promise.resolve(new Response(
      String(url).includes("top_up")
        ? JSON.stringify({ status: "requested", amount_cents: 2000 })
        : JSON.stringify({ balance_microcents: 0, balance_dollars: "0.00" }),
      { status: 200 },
    )));
    global.fetch = fetchMock;
    orgId = "org_credits";
    const { result } = renderHook(() => useCredits(), { wrapper });

    await act(async () => { await result.current.startTopUp(2000); });
    await act(async () => { await result.current.startTopUp(2000); });
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes("top_up"));
    const first = JSON.parse(calls[0][1].body);
    const second = JSON.parse(calls[1][1].body);
    expect(first.amount_cents).toBe(2000);
    expect(second).toEqual(first);
    expect(first.request_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("does not read a workspace balance without an active workspace", () => {
    orgId = null as unknown as string;
    const fetchMock = vi.fn();
    global.fetch = fetchMock;
    renderHook(() => useCredits(), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();
    orgId = "org_1";
  });
});
