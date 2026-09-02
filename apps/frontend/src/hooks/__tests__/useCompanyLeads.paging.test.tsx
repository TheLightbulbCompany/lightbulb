// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";

const get = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get }) }));

import { useCompanyLeads, type LeadRow } from "../useCompanyLeads";

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>
);

const person = (id: string): LeadRow => ({
  id,
  first_name: id,
  last_name: "L.",
  title: null,
  organization: null,
  has_email: false,
  city: null,
  country: null,
});
const page = (n: number, hasMore = true) => ({
  connected: true,
  data: [person(`p${n}a`), person(`p${n}b`)],
  total: 6,
  page: n,
  has_more: hasMore,
});

beforeEach(() => get.mockReset());

describe("useCompanyLeads paging", () => {
  it("never shows the current page twice while the next one loads", async () => {
    let releasePage2!: (v: unknown) => void;
    get.mockImplementation((path: string) =>
      path.endsWith("page=1")
        ? Promise.resolve(page(1))
        : new Promise((resolve) => {
            releasePage2 = resolve;
          }),
    );
    const { result } = renderHook(() => useCompanyLeads(), { wrapper });
    act(() => result.current.search("founders"));
    await waitFor(() => expect(result.current.rows).toHaveLength(2));

    act(() => result.current.next());
    // Page 1 moved into `earlier`; nothing for page 2 yet — two rows, not four,
    // and the total the owner was reading stays put.
    expect(result.current.rows.map((r) => r.id)).toEqual(["p1a", "p1b"]);
    expect(result.current.total).toBe(6);

    await act(async () => releasePage2(page(2, false)));
    await waitFor(() => expect(result.current.rows).toHaveLength(4));
    expect(result.current.hasMore).toBe(false);
  });

  it("retries a failed page in place instead of re-appending and skipping it", async () => {
    let failures = 1;
    get.mockImplementation((path: string) => {
      if (path.endsWith("page=1")) return Promise.resolve(page(1));
      if (failures-- > 0) return Promise.reject(new ApiError(502, { detail: { code: "apollo_unavailable" } }));
      return Promise.resolve(page(2, false));
    });
    const { result } = renderHook(() => useCompanyLeads(), { wrapper });
    act(() => result.current.search("founders"));
    await waitFor(() => expect(result.current.rows).toHaveLength(2));

    act(() => result.current.next());
    await waitFor(() => expect(result.current.moreFailed).toBe(true));
    // The table the owner had is still there, and the button still offers.
    expect(result.current.rows.map((r) => r.id)).toEqual(["p1a", "p1b"]);
    expect(result.current.view).toEqual({ kind: "rows" });
    expect(result.current.hasMore).toBe(true);
    expect(result.current.total).toBe(6);

    act(() => result.current.next());
    await waitFor(() => expect(result.current.rows).toHaveLength(4));
    // Page 2 landed once; page 1 was not duplicated; nothing was skipped.
    expect(result.current.rows.map((r) => r.id)).toEqual(["p1a", "p1b", "p2a", "p2b"]);
    expect(get.mock.calls.map(([p]) => p.split("page=")[1])).toEqual(["1", "2", "2"]);
  });
});
