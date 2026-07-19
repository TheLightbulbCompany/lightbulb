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

describe("presence touch-scroll exclusion", () => {
  it("a touch pointerdown (scroll start) does NOT count as input, but a click (tap) does", () => {
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(3_000_000);
    // Establish a stale baseline via a keydown far in the past.
    window.dispatchEvent(new Event("keydown"));
    nowSpy.mockReturnValue(3_000_000 + 200_000);
    // A touch-scroll's pointerdown must be ignored — age keeps growing.
    const touch = new Event("pointerdown") as PointerEvent;
    Object.defineProperty(touch, "pointerType", { value: "touch" });
    window.dispatchEvent(touch);
    expect(lastInputAgeMs()).toBe(200_000);
    // The click that follows a real tap DOES count.
    window.dispatchEvent(new Event("click"));
    expect(lastInputAgeMs()).toBe(0);
    nowSpy.mockRestore();
  });

  it("a mouse pointerdown (non-touch) still counts", () => {
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(4_000_000);
    const mouse = new Event("pointerdown") as PointerEvent;
    Object.defineProperty(mouse, "pointerType", { value: "mouse" });
    window.dispatchEvent(mouse);
    expect(lastInputAgeMs()).toBe(0);
    nowSpy.mockRestore();
  });
});
