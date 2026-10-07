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

  it("round-trips the hours after fall-back on EST (-05:00), not the offset the naive instant reads", () => {
    // 2026-11-01 falls back at 2:00 EDT -> 1:00 EST. 03:00 that morning is
    // EST; a single offset read at the naive instant still reports EDT.
    const iso = fromEasternLocal("2026-11-01T03:00");
    expect(iso).toBe("2026-11-01T03:00:00-05:00");
    expect(toEasternLocal(iso)).toBe("2026-11-01T03:00");
  });

  it("round-trips the hours after spring-forward on EDT (-04:00)", () => {
    // 2027-03-14 springs forward at 2:00 EST -> 3:00 EDT. 05:00 is EDT.
    const iso = fromEasternLocal("2027-03-14T05:00");
    expect(iso).toBe("2027-03-14T05:00:00-04:00");
    expect(toEasternLocal(iso)).toBe("2027-03-14T05:00");
  });

  it("resolves an ambiguous fall-back wall time to its first (EDT) occurrence", () => {
    // 2026-11-01T01:30 happens twice. The first one is EDT.
    const iso = fromEasternLocal("2026-11-01T01:30");
    expect(iso).toBe("2026-11-01T01:30:00-04:00");
    expect(toEasternLocal(iso)).toBe("2026-11-01T01:30");
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

  it("renders ET midnight as 00:00, not 24:00", () => {
    // 2026-07-04T00:00 ET is EDT (-04:00) -> 2026-07-04T04:00:00Z. `en-US`
    // with `hour12: false` can resolve to the `h24` hour cycle, which prints
    // midnight as "24:00" — an invalid `datetime-local` value. `hourCycle:
    // "h23"` pins it to "00".
    expect(toEasternLocal("2026-07-04T04:00:00Z")).toBe("2026-07-04T00:00");
  });

  it("renders ET midnight as 00:00 on the November DST-transition date", () => {
    // 2026-11-01 falls back at 2:00 EDT -> 1:00 EST, so midnight itself is
    // still EDT (-04:00): 2026-11-01T00:00 ET = 2026-11-01T04:00:00Z.
    expect(toEasternLocal("2026-11-01T04:00:00Z")).toBe("2026-11-01T00:00");
  });
});
