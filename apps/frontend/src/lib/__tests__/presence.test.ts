// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import { isPresent, lastInputAgeMs } from "@/lib/presence";

describe("presence", () => {
  it("fresh module load counts as recent input (mount-wake presence)", () => {
    expect(lastInputAgeMs()).toBeLessThan(5_000);
  });

  it("input events reset the recency clock", () => {
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(1_000_000);
    window.dispatchEvent(new Event("pointerdown"));
    nowSpy.mockReturnValue(1_030_000);
    expect(lastInputAgeMs()).toBe(30_000);
    window.dispatchEvent(new Event("keydown"));
    expect(lastInputAgeMs()).toBe(0);
    nowSpy.mockRestore();
  });

  it("hidden tab is never present regardless of input recency", () => {
    window.dispatchEvent(new Event("pointerdown"));
    const spy = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("hidden");
    expect(isPresent()).toBe(false);
    spy.mockRestore();
  });

  it("visible tab with stale input is not present", () => {
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(2_000_000);
    window.dispatchEvent(new Event("wheel"));
    nowSpy.mockReturnValue(2_000_000 + 121_000);
    const vis = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("visible");
    expect(isPresent()).toBe(false);
    nowSpy.mockReturnValue(2_000_000 + 60_000);
    expect(isPresent()).toBe(true);
    vis.mockRestore();
    nowSpy.mockRestore();
  });
});
