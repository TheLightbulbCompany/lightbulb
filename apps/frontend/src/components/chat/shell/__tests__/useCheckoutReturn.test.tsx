// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import React from "react";

// `?credits=bundled` is the flag CreditsStep reads to show "Adding your Claude
// credits" instead of a live second purchase form. This hook used to strip ANY
// `credits` value on mount, so the flag never survived to be read — an owner
// who had just paid $150 in one Checkout was immediately offered another one.
// Caught only on dev; every unit test passed the prop to CreditsStep directly
// and so never went near this hook.

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
  });

  it("preserves credits=bundled so CreditsStep can render the waiting state", () => {
    nav.search = "provider=bedrock_claude&credits=bundled";
    renderHook(() => useCheckoutReturn());
    // Either no rewrite at all, or one that KEEPS the flag.
    for (const call of replaceSearchParams.mock.calls) {
      expect((call[0] as URLSearchParams).get("credits")).toBe("bundled");
    }
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

  it("strips checkout=success while keeping both provider and the bundled flag", () => {
    nav.search = "checkout=success&provider=bedrock_claude&credits=bundled";
    renderHook(() => useCheckoutReturn());
    const params = replaceSearchParams.mock.calls.at(-1)![0] as URLSearchParams;
    expect(params.get("checkout")).toBeNull();
    expect(params.get("provider")).toBe("bedrock_claude");
    expect(params.get("credits")).toBe("bundled");
  });
});
