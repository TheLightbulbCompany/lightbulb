import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const swrKeys: string[] = [];
// Keys the fake SWR answers as "still fetching, nothing cached" — what real
// SWR returns for an unseen key when keepPreviousData is off.
const pendingKeys = new Set<string>();
const swrOptions: ({ keepPreviousData?: boolean } | undefined)[] = [];
vi.mock("swr", () => ({
  default: (key: string, _fetcher: unknown, options?: { keepPreviousData?: boolean }) => {
    swrKeys.push(key);
    swrOptions.push(options);
    if (pendingKeys.has(key)) return { data: undefined, error: undefined, isLoading: true };
    return {
      data: { stripe_bound: true, data: [{ id: `row-${swrKeys.length}` }], has_more: true, next_cursor: "cur_1" },
      error: undefined,
      isLoading: false,
    };
  },
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn() }) }));

import { useCompanyMoney } from "../useCompanyMoney";

describe("useCompanyMoney — day window", () => {
  beforeEach(() => { swrKeys.length = 0; swrOptions.length = 0; pendingKeys.clear(); });

  it("reads the bare list without a window, exactly as before", () => {
    renderHook(() => useCompanyMoney("payments"));
    expect(swrKeys[0]).toBe("/company/payments");
  });

  it("puts start and end on the key when a window is given", () => {
    renderHook(() => useCompanyMoney("payments", { start: "2026-09-05", end: "2026-09-05" }));
    expect(swrKeys[0]).toBe("/company/payments?start=2026-09-05&end=2026-09-05");
  });

  it("pages inside the window and resets the pages when the window changes", () => {
    const { result, rerender } = renderHook(
      ({ w }: { w?: { start: string; end: string } }) => useCompanyMoney("payments", w),
      { initialProps: { w: { start: "2026-09-05", end: "2026-09-05" } } },
    );
    act(() => result.current.next());
    expect(swrKeys.at(-1)).toBe("/company/payments?start=2026-09-05&end=2026-09-05&starting_after=cur_1");
    expect(result.current.rows.length).toBe(2);
    rerender({ w: { start: "2026-09-06", end: "2026-09-06" } });
    expect(swrKeys.at(-1)).toBe("/company/payments?start=2026-09-06&end=2026-09-06");
    expect(result.current.rows.length).toBe(1);
  });

  it("shows loading on a window switch, not the previous day's rows", () => {
    // keepPreviousData is for PAGING (page 1 must not blank while page 2
    // loads). Left on across a window change it renders Sept 5's payments
    // under a "Payments on Sept 6" heading — a wrong day's money.
    pendingKeys.add("/company/payments?start=2026-09-06&end=2026-09-06");
    const { result, rerender } = renderHook(
      ({ w }: { w: { start: string; end: string } }) => useCompanyMoney("payments", w),
      { initialProps: { w: { start: "2026-09-05", end: "2026-09-05" } } },
    );
    expect(result.current.view.kind).toBe("rows");
    expect(swrOptions.at(-1)?.keepPreviousData).toBe(false);

    rerender({ w: { start: "2026-09-06", end: "2026-09-06" } });
    expect(result.current.view.kind).toBe("loading");
    expect(result.current.rows).toEqual([]);
  });

  it("keeps the previous page while paging, so page 1 does not blank", () => {
    const { result } = renderHook(() => useCompanyMoney("payments"));
    expect(swrOptions.at(-1)?.keepPreviousData).toBe(false);
    act(() => result.current.next());
    expect(swrOptions.at(-1)?.keepPreviousData).toBe(true);
  });
});
