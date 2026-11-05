// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import React from "react";

const nav = vi.hoisted(() => ({ search: "" }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.search),
}));

const billing = vi.hoisted(() => ({ refresh: vi.fn(), isSubscribed: true }));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ refresh: billing.refresh, isSubscribed: billing.isSubscribed }),
}));

const replaceSearchParams = vi.hoisted(() => vi.fn());
vi.mock("@/lib/shellNav", () => ({ replaceSearchParams }));

import { useCheckoutReturn } from "../useCheckoutReturn";
import { recentCheckoutReturn } from "@/lib/checkoutReturn";

describe("useCheckoutReturn", () => {
  beforeEach(() => {
    replaceSearchParams.mockClear();
    billing.refresh.mockClear();
    billing.isSubscribed = true;
  });

  it("refreshes immediately and stops bounded polling once the webhook makes billing active", () => {
    vi.useFakeTimers();
    nav.search = "checkout=success";
    billing.isSubscribed = false;
    const { rerender } = renderHook(() => useCheckoutReturn());
    expect(billing.refresh).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(2_000));
    expect(billing.refresh).toHaveBeenCalledTimes(2);
    billing.isSubscribed = true;
    rerender();
    const refreshesAfterActive = billing.refresh.mock.calls.length;
    act(() => vi.advanceTimersByTime(30_000));
    expect(billing.refresh).toHaveBeenCalledTimes(refreshesAfterActive);
    vi.useRealTimers();
  });

  it("remembers the return for the tab before stripping ?checkout= (a late-mounting ChatGPT step reads it)", () => {
    sessionStorage.clear();
    nav.search = "checkout=success&provider=chatgpt_oauth";
    renderHook(() => useCheckoutReturn());
    expect(recentCheckoutReturn()).toBe(true);
    expect(replaceSearchParams).toHaveBeenCalled();
    expect(recentCheckoutReturn(Date.now() + 11 * 60_000)).toBe(false);
  });
});
