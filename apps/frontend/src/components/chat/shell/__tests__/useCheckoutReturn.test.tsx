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

describe("useCheckoutReturn — credits param", () => {
  beforeEach(() => {
    replaceSearchParams.mockClear();
    billing.refresh.mockClear();
    billing.isSubscribed = true;
  });

  it("strips obsolete credits=bundled markers without changing the provider step", () => {
    nav.search = "provider=bedrock_claude&credits=bundled";
    renderHook(() => useCheckoutReturn());
    const params = replaceSearchParams.mock.calls.at(-1)![0] as URLSearchParams;
    expect(params.get("credits")).toBeNull();
    expect(params.get("provider")).toBe("bedrock_claude");
  });

  it("still strips the real return markers", () => {
    for (const value of ["success", "cancel"]) {
      replaceSearchParams.mockClear();
      nav.search = `provider=bedrock_claude&credits=${value}`;
      renderHook(() => useCheckoutReturn());
      expect(replaceSearchParams).toHaveBeenCalled();
      const params = replaceSearchParams.mock.calls.at(-1)![0] as URLSearchParams;
      expect(params.get("credits")).toBeNull();
      // ?provider= must survive — the wizard needs it to pick the provider step.
      expect(params.get("provider")).toBe("bedrock_claude");
    }
  });

  it("strips checkout=success and obsolete bundled markers together", () => {
    nav.search = "checkout=success&provider=bedrock_claude&credits=bundled";
    renderHook(() => useCheckoutReturn());
    const params = replaceSearchParams.mock.calls.at(-1)![0] as URLSearchParams;
    expect(params.get("checkout")).toBeNull();
    expect(params.get("provider")).toBe("bedrock_claude");
    expect(params.get("credits")).toBeNull();
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
});
