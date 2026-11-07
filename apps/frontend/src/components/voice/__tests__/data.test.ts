import { describe, expect, it } from "vitest";
import { callNeedsYou, mmss, nameOf } from "../data";

describe("voice data helpers", () => {
  it("a call needs you until it is resolved, and only for the two waiting outcomes", () => {
    const call = { type: "call" as const, roomId: "r", at: 0 };
    expect(callNeedsYou({ ...call, outcome: "needs_you" })).toBe(true);
    expect(callNeedsYou({ ...call, outcome: "handoff_missed" })).toBe(true);
    expect(callNeedsYou({ ...call, outcome: "needs_you", resolvedAt: "2026-11-07" })).toBe(false);
    expect(callNeedsYou({ ...call, outcome: "booked" })).toBe(false);
    expect(callNeedsYou({ type: "meeting", roomId: "m", at: 0 })).toBe(false);
  });
  it("formats durations and falls back to Lightbulb for an unnamed assistant", () => {
    expect(mmss(160)).toBe("2:40");
    expect(mmss(9)).toBe("0:09");
    expect(mmss(undefined)).toBe("");
    expect(nameOf({ name: "  " })).toBe("Lightbulb");
    expect(nameOf({ name: "Nova" })).toBe("Nova");
  });
});
