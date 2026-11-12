import { describe, it, expect, vi, afterEach } from "vitest";
import { timeAgo } from "@/lib/timeAgo";

const NOW = new Date("2026-08-24T12:00:00Z");

function at(offsetMs: number): string {
  return new Date(NOW.getTime() + offsetMs).toISOString();
}

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

describe("timeAgo", () => {
  afterEach(() => vi.useRealTimers());

  function freeze() {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  }

  it("formats past instants unchanged (upstream behaviour)", () => {
    freeze();
    expect(timeAgo(at(-5 * MIN))).toBe("5m ago");
    expect(timeAgo(at(-3 * HOUR))).toBe("3h ago");
    expect(timeAgo(at(-2 * DAY))).toBe("2d ago");
    expect(timeAgo(at(-10 * DAY))).toBe("1w ago");
    expect(timeAgo(at(-90 * DAY))).toBe("3mo ago");
  });

  it("formats future instants forwards, not as 'just now'", () => {
    // The bug this guards: a monitor's next check is always in the FUTURE, and
    // upstream's past-only subtraction rendered every one of them "just now".
    freeze();
    expect(timeAgo(at(5 * MIN))).toBe("in 5m");
    expect(timeAgo(at(3 * HOUR))).toBe("in 3h");
    expect(timeAgo(at(2 * DAY))).toBe("in 2d");
    expect(timeAgo(at(365 * DAY))).toBe("in 12mo");
  });

  it("treats the minute either side of now as 'just now'", () => {
    freeze();
    expect(timeAgo(at(-1000))).toBe("just now");
    expect(timeAgo(at(1000))).toBe("just now");
  });

  it("returns empty string for an unparseable date rather than 'just now'", () => {
    expect(timeAgo("not a date")).toBe("");
  });
});
