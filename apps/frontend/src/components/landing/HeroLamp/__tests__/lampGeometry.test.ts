import { describe, expect, it } from "vitest";
import {
  BLADES,
  COLLAR_RINGS,
  GLASS_HEIGHT,
  GLASS_MAX_RADIUS,
  ST64_PROFILE,
  TIE_HEIGHTS,
  cssColor,
} from "../lampGeometry";

describe("ST64 profile (§02: straight sides rising to one elliptical dome)", () => {
  it("runs from the neck at the bottom to a closed apex at the top", () => {
    const first = ST64_PROFILE[0];
    const last = ST64_PROFILE[ST64_PROFILE.length - 1];
    expect(first[1]).toBe(0);
    expect(last[0]).toBe(0);
    expect(last[1]).toBe(GLASS_HEIGHT);
  });

  it("height is strictly increasing so the lathe cannot fold on itself", () => {
    for (let i = 1; i < ST64_PROFILE.length; i += 1) {
      expect(ST64_PROFILE[i][1]).toBeGreaterThan(ST64_PROFILE[i - 1][1]);
    }
  });

  it("widest point is the shoulder, not the neck and not the dome", () => {
    const widest = ST64_PROFILE.reduce((a, b) => (b[0] > a[0] ? b : a));
    expect(widest[0]).toBe(GLASS_MAX_RADIUS);
    expect(widest[1]).toBeGreaterThan(GLASS_HEIGHT * 0.5);
    expect(widest[1]).toBeLessThan(GLASS_HEIGHT * 0.85);
  });
});

describe("filament (§02: three blades, tied twice)", () => {
  it("is exactly three blades, symmetric about the centre one", () => {
    expect(BLADES).toHaveLength(3);
    expect(BLADES.map((b) => b.tiltDeg)).toEqual([-12, 0, 12]);
  });
  it("has exactly two ties, both inside the blade span", () => {
    expect(TIE_HEIGHTS).toHaveLength(2);
    for (const h of TIE_HEIGHTS) {
      expect(h).toBeGreaterThan(0);
      expect(h).toBeLessThan(1);
    }
  });
});

describe("collar", () => {
  it("is three stepped rings that narrow downward", () => {
    expect(COLLAR_RINGS).toHaveLength(3);
    expect(COLLAR_RINGS[0][0]).toBeGreaterThan(COLLAR_RINGS[1][0]);
    expect(COLLAR_RINGS[1][0]).toBeGreaterThan(COLLAR_RINGS[2][0]);
  });
});

describe("cssColor", () => {
  it("returns the fallback when the variable is unset", () => {
    expect(cssColor("--bulb", "#F0C34E")).toBe("#F0C34E");
  });
  it("reads a set variable", () => {
    document.documentElement.style.setProperty("--bulb", "#123456");
    expect(cssColor("--bulb", "#F0C34E")).toBe("#123456");
    document.documentElement.style.removeProperty("--bulb");
  });
});
