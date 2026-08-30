import { describe, expect, it } from "vitest";

import { fromEasternLocal, toEasternLocal } from "../etTime";

describe("fromEasternLocal / toEasternLocal round trip", () => {
  it("round-trips a July date on EDT (-04:00)", () => {
    const iso = fromEasternLocal("2026-07-15T10:00");
    expect(iso).toBe("2026-07-15T10:00:00-04:00");
    expect(toEasternLocal(iso)).toBe("2026-07-15T10:00");
  });

  it("round-trips a January date on EST (-05:00)", () => {
    const iso = fromEasternLocal("2026-01-15T10:00");
    expect(iso).toBe("2026-01-15T10:00:00-05:00");
    expect(toEasternLocal(iso)).toBe("2026-01-15T10:00");
  });

  it("resolves a March DST-gap wall time (2026-03-08T02:30 never occurs on a real ET clock) to the pre-transition EST offset instead of crashing", () => {
    // 2026-03-08 is the spring-forward Sunday — clocks jump from 2:00 EST
    // straight to 3:00 EDT, so 02:30 has no real ET instant. Documented
    // behaviour (see etTime.ts): resolves to -05:00, doesn't throw.
    expect(fromEasternLocal("2026-03-08T02:30")).toBe("2026-03-08T02:30:00-05:00");
  });
});

describe("toEasternLocal", () => {
  it("returns an empty string for null", () => {
    expect(toEasternLocal(null)).toBe("");
  });

  it("converts a UTC ISO string to ET wall time", () => {
    // 2026-07-15T14:00:00Z is 10:00 EDT.
    expect(toEasternLocal("2026-07-15T14:00:00Z")).toBe("2026-07-15T10:00");
  });
});
