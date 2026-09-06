import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const swrKeys: string[] = [];
vi.mock("swr", () => ({
  default: (key: string) => {
    swrKeys.push(key);
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
  beforeEach(() => { swrKeys.length = 0; });

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
});
