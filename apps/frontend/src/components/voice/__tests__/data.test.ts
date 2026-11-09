import { describe, expect, it } from "vitest";
import { callNeedsYou, callSummary, mmss, nameOf } from "../data";

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

describe("monthMinutes", () => {
  it("sums this month's call seconds to whole minutes", async () => {
    const { monthMinutes } = await import("../data");
    const now = new Date(2026, 10, 7, 12);
    const c = (at: Date, durationSec: number) => ({ type: "call" as const, roomId: "r", at: at.getTime(), durationSec });
    expect(monthMinutes([c(new Date(2026, 10, 2), 1200), c(new Date(2026, 10, 5), 1080), c(new Date(2026, 9, 30), 6000)], now)).toBe(38);
  });
});

it("uses the latest recorded non-guessed step when a call summary is empty", () => {
  const steps = [{ text: "Booked Tuesday at 10" }, { text: "Maybe completed", guessed: true }];
  expect(callSummary({ steps })).toBe("Booked Tuesday at 10");
  expect(callSummary({ summary: "Confirmed with caller", steps })).toBe("Confirmed with caller");
  expect(callSummary({ steps: [{ text: "Maybe completed", guessed: true }] })).toBe("Open conversation");
});
